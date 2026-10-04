"""
backtest_five_rule.py — honest walk-forward test of the 5-Rule SMC model on YOUR NIFTY 1m candles.

What it does
  1. Replays every stored session minute by minute (closed candles only, no look-ahead) and asks
     smc_five_rule.evaluate() "would you TAKE a trade now?" exactly as the live app does.
  2. Simulates each trade on the following 1m bars: SL first if SL and TP fall in the same bar,
     exit at 15:15 if neither is hit, cost deducted per trade (index points).
  3. Compares against two RANDOM controls with the same risk/reward geometry:
        A · random TIME   — same day, same window, same side, random minute   (does the setup timing matter?)
        B · random SIDE   — same minute, same entry, coin-flip BUY/SELL         (does the bias matter?)
  4. Extra checks: window 09:30–11:30 vs 13:30–14:45 vs outside, min R:R 1–4, exit styles
     (full TP2 · breakeven at +1R · 50% at TP1 then breakeven), market vs limit entry.
  5. Session-sweep check: how often NIFTY takes BOTH sides of the opening range / first hour,
     versus the same day's moves shuffled (random control), split by how big the opening range was.

Run (from the OC folder):
  .venv/bin/python backtest_five_rule.py                 # uses data/candles/*.csv (+ data/candles_hist)
  .venv/bin/python backtest_five_rule.py --fetch 365     # first downloads ~1 year of 1m NIFTY from Dhan
  options: --reps 500  --workers 4  --step 1  --rescan

Out-of-sample check (score ONLY sessions the first test never saw, with the entry style fixed in advance):
  .venv/bin/python backtest_five_rule.py --fetch 730 --before 2025-10-03 --entry limit --tag oos
  → writes five_rule_oos.json / five_rule_oos_trades.csv; the live panels keep reading five_rule.json.

Output: data/backtests/five_rule.json, five_rule_trades.csv, five_rule_signals.csv (cache).
Information only — not financial advice.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, time as dtime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
OUT_DIR = ROOT / "data" / "backtests"
HIST_DIR = ROOT / "data" / "candles_hist"
SIG_CACHE = OUT_DIR / "five_rule_signals.csv"
WARMUP_DAYS = 5          # 15m structure needs history before the first scored day
HIST_DAYS = 10           # sessions of history passed to evaluate() for each day
COST_PTS = 1.0           # round-trip cost/slippage in index points per trade
EOD = dtime(15, 15)      # flat by 15:15
LAST_ENTRY = dtime(15, 0)


# ═══════════════════════════════════════════════════════════════════════════
# Data
# ═══════════════════════════════════════════════════════════════════════════
def _read_day(f: Path) -> pd.DataFrame | None:
    try:
        d = pd.read_csv(f)
        d.columns = [c.strip().lower() for c in d.columns]
        d["timestamp"] = pd.to_datetime(d["timestamp"], errors="coerce")
        d = d.dropna(subset=["timestamp", "open", "high", "low", "close"])
        t = d["timestamp"].dt.time
        d = d[(t >= dtime(9, 15)) & (t < dtime(15, 30))]
        if len(d) < 300 or d["close"].nunique() < 50:      # partial or placeholder day
            return None
        return d[["timestamp", "open", "high", "low", "close"]].sort_values("timestamp")
    except Exception:
        return None


def candle_dirs() -> list[Path]:
    dirs = [HIST_DIR]
    try:
        import config
        dirs.append(Path(config.CANDLE_DIR))
    except Exception:
        dirs.append(ROOT / "data" / "candles")
    return dirs


def load_1m() -> pd.DataFrame:
    days = {}
    for folder in candle_dirs():                 # later folders (live candles) win for the same day
        if not folder.exists():
            continue
        for f in sorted(folder.glob("20*.csv")):
            d = _read_day(f)
            if d is not None:
                days[d["timestamp"].iloc[0].date()] = d
    if not days:
        return pd.DataFrame()
    return pd.concat([days[k] for k in sorted(days)], ignore_index=True)


def fetch_history(days_back: int) -> int:
    """Download 1m NIFTY index candles from Dhan in 85-day chunks → data/candles_hist/YYYY-MM-DD.csv."""
    import config
    import dhan_client as dc
    HIST_DIR.mkdir(parents=True, exist_ok=True)
    end = datetime.now().replace(hour=15, minute=30, second=0, microsecond=0)
    start_all = end - timedelta(days=days_back)
    saved, cur = 0, start_all
    while cur < end:
        stop = min(cur + timedelta(days=85), end)
        try:
            payload = dc._post("/charts/intraday", {
                "securityId": str(config.DHAN_INDEX_SECURITY_ID), "exchangeSegment": "IDX_I",
                "instrument": "INDEX", "interval": "1", "oi": False,
                "fromDate": cur.strftime("%Y-%m-%d 09:15:00"), "toDate": stop.strftime("%Y-%m-%d 15:30:00")})
            df = dc._to_frame(payload)
        except Exception as e:                               # noqa: BLE001
            print(f"  ! Dhan fetch {cur:%Y-%m-%d}→{stop:%Y-%m-%d} failed: {e}")
            df = pd.DataFrame()
        if len(df):
            df["day"] = df["timestamp"].dt.date
            for day, g in df.groupby("day"):
                p = HIST_DIR / f"{day:%Y-%m-%d}.csv"
                g.drop(columns="day").assign(timestamp=g["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")).to_csv(p, index=False)
                saved += 1
            print(f"  fetched {cur:%Y-%m-%d} → {stop:%Y-%m-%d}: {df['day'].nunique()} sessions")
        cur = stop + timedelta(days=1)
    return saved


def resample(df: pd.DataFrame, m: int) -> pd.DataFrame:
    out = []
    for _, g in df.groupby(df["timestamp"].dt.date):
        r = (g.set_index("timestamp").resample(f"{m}min", origin="start_day", offset="15min")
             .agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna().reset_index())
        out.append(r)
    return pd.concat(out, ignore_index=True)


# ═══════════════════════════════════════════════════════════════════════════
# Signal scan (slow part — runs in parallel, cached)
# ═══════════════════════════════════════════════════════════════════════════
def _install_cache():
    """Memoise smc.read / FVG_Tracker inside the backtest process only (15m & 5m inputs repeat for many minutes)."""
    import smc
    if getattr(smc, "_bt_cached", False):
        return
    raw_read, raw_fvg = smc.read, smc.FVG_Tracker
    store_r, store_f = {}, {}

    def key(df, extra):
        c = df["close"].to_numpy(float)
        ts = df["timestamp"].iloc[-1] if "timestamp" in df and len(df) else len(df)
        return (len(df), str(ts), float(c.sum()) if len(c) else 0.0, extra)

    def read(df, n=2, *a, **k):
        if a or k:
            return raw_read(df, n, *a, **k)
        kk = key(df, n)
        if kk not in store_r:
            if len(store_r) > 4000:
                store_r.clear()
            store_r[kk] = raw_read(df, n)
        return store_r[kk]

    def fvg(df, *a, **k):
        if a or k:
            return raw_fvg(df, *a, **k)
        kk = key(df, "f")
        if kk not in store_f:
            if len(store_f) > 4000:
                store_f.clear()
            store_f[kk] = raw_fvg(df)
        return store_f[kk]

    smc.read, smc.FVG_Tracker, smc._bt_cached = read, fvg, True


def scan_day(args) -> list[dict]:
    day, hist, daily, rr, step = args
    _install_cache()
    import smc_five_rule as fr
    d5a, d15a = resample(hist, 5), resample(hist, 15)
    t1 = hist["timestamp"]
    out = []
    first = pd.Timestamp(f"{day} 09:31")
    last = pd.Timestamp(f"{day} {LAST_ENTRY:%H:%M}")
    now = first
    while now <= last:
        one = hist[t1 < now]
        if len(one) and one["timestamp"].iloc[-1].date() == day:
            d5 = d5a[d5a["timestamp"] + pd.Timedelta(minutes=5) <= now].tail(225).reset_index(drop=True)
            d15 = d15a[d15a["timestamp"] + pd.Timedelta(minutes=15) <= now].tail(250).reset_index(drop=True)
            if len(d5) > 30 and d5["timestamp"].iloc[-1].date() == day:
                try:
                    f = fr.evaluate(one.tail(150).reset_index(drop=True), d5, d15, now.to_pydatetime(), daily, rr)
                except Exception:
                    f = None
                if f is not None and f.verdict.startswith("TAKE") and f.entry and f.sl and f.tp:
                    det5 = f.rules[4][2] if len(f.rules) > 4 else ""
                    sw_t, ch_t = f.levels.get("sweep_t"), f.levels.get("choch_t")
                    out.append(dict(day=str(day), now=now.strftime("%Y-%m-%d %H:%M:%S"),
                                    side=f.side, lo=f.entry[0], hi=f.entry[1], sl=f.sl, tp=f.tp,
                                    tp1=f.levels.get("TP1"), rr=f.rr,
                                    kind=det5.split()[1] if det5.startswith("5m ") else "",
                                    sweep_age=(now - pd.Timestamp(sw_t)).total_seconds() / 60 if sw_t is not None else np.nan,
                                    choch_age=(now - pd.Timestamp(ch_t)).total_seconds() / 60 if ch_t is not None else np.nan))
        now += pd.Timedelta(minutes=step)
    return out


def in_range(day, after=None, before=None) -> bool:
    return (after is None or day >= after) and (before is None or day < before)


def scan_all(m1: pd.DataFrame, step: int, workers: int, after=None, before=None) -> pd.DataFrame:
    import smc_five_rule as fr
    rr = fr._rules()
    rr["windows"] = [["09:15", "15:30"]]       # rule 2 is applied later, per window group
    rr["min_rr"] = 1.0                          # R:R threshold applied later (sweep)
    daily = fr.best_daily(m1)
    days = sorted(m1["timestamp"].dt.date.unique())
    tasks = []
    step_f = 1.0 / max(1, step)
    for i, day in enumerate(days):
        if i < WARMUP_DAYS or not in_range(day, after, before):
            continue
        keep = set(days[max(0, i - HIST_DAYS): i + 1])
        hist = m1[m1["timestamp"].dt.date.isin(keep)].reset_index(drop=True)
        tasks.append((day, hist, daily, rr, step))
    print(f"Scanning {len(tasks)} sessions minute by minute on {workers} worker(s)… "
          f"(roughly {max(1, int(len(tasks) * 20 * step_f / max(1, workers) / 60))} min; cached afterwards)")
    rows = []
    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            for k, res in enumerate(ex.map(scan_day, tasks, chunksize=1), 1):
                rows += res
                if k % 10 == 0 or k == len(tasks):
                    print(f"  {k}/{len(tasks)} sessions scanned · {len(rows)} TAKE-minutes so far")
    else:
        for k, t in enumerate(tasks, 1):
            rows += scan_day(t)
            if k % 5 == 0 or k == len(tasks):
                print(f"  {k}/{len(tasks)} sessions scanned · {len(rows)} TAKE-minutes so far")
    return pd.DataFrame(rows)


# ═══════════════════════════════════════════════════════════════════════════
# Trade simulation
# ═══════════════════════════════════════════════════════════════════════════
class Day:
    def __init__(self, g: pd.DataFrame):
        self.t = g["timestamp"].reset_index(drop=True)
        self.o, self.h = g["open"].to_numpy(float), g["high"].to_numpy(float)
        self.l, self.c = g["low"].to_numpy(float), g["close"].to_numpy(float)
        tt = self.t.dt.time
        self.eod = int(np.searchsorted(tt.to_numpy(), EOD, side="right")) - 1
        self.eod = max(0, min(self.eod, len(self.c) - 1))

    def idx(self, ts) -> int:
        return int(np.searchsorted(self.t.to_numpy(), np.datetime64(pd.Timestamp(ts)), side="left"))


def _first(mask: np.ndarray) -> int:
    return int(np.argmax(mask)) if mask.any() else 10 ** 9


def _run(D: Day, start: int, sgn: int, e: float, sl: float, tp: float, tp1, mode: str):
    """Returns (pnl_pts, exit_index, outcome). SL wins ties. start = first bar the position is live."""
    end = D.eod
    if start > end:
        return None
    H, L = D.h[start:end + 1], D.l[start:end + 1]
    risk = sgn * (e - sl)
    hit_sl = (L <= sl) if sgn > 0 else (H >= sl)
    hit_tp = (H >= tp) if sgn > 0 else (L <= tp)
    i_sl, i_tp = _first(hit_sl), _first(hit_tp)

    def eod_exit(frac=1.0, bank=0.0):
        return bank + frac * sgn * (D.c[end] - e), end, "EOD"

    if mode == "half_tp1" and tp1 is not None and sgn * (tp1 - e) > 0 and sgn * (tp - tp1) > 0:
        hit1 = (H >= tp1) if sgn > 0 else (L <= tp1)
        i1 = _first(hit1)
        if i_sl <= i1:
            return (-risk, start + i_sl, "SL") if i_sl < 10 ** 9 else eod_exit()
        if i1 >= 10 ** 9:
            return eod_exit()
        bank = 0.5 * sgn * (tp1 - e)
        if i_tp == i1:
            return bank + 0.5 * sgn * (tp - e), start + i1, "TP1+TP2"
        H2, L2 = H[i1 + 1:], L[i1 + 1:]
        j_be = _first((L2 <= e) if sgn > 0 else (H2 >= e))
        j_tp = _first((H2 >= tp) if sgn > 0 else (L2 <= tp))
        if j_be < 10 ** 9 and j_be <= j_tp:
            return bank, start + i1 + 1 + j_be, "TP1+BE"
        if j_tp < 10 ** 9:
            return bank + 0.5 * sgn * (tp - e), start + i1 + 1 + j_tp, "TP1+TP2"
        return eod_exit(0.5, bank)

    if mode == "be1r":
        trig = e + sgn * risk
        i_tr = _first((H >= trig) if sgn > 0 else (L <= trig))
        if i_sl <= i_tr:
            return (-risk, start + i_sl, "SL") if i_sl < 10 ** 9 else eod_exit()
        if i_tr >= 10 ** 9:
            return eod_exit()
        if i_tp == i_tr:
            return sgn * (tp - e), start + i_tp, "TP"
        H2, L2 = H[i_tr + 1:], L[i_tr + 1:]
        j_be = _first((L2 <= e) if sgn > 0 else (H2 >= e))
        j_tp = _first((H2 >= tp) if sgn > 0 else (L2 <= tp))
        if j_be < 10 ** 9 and j_be <= j_tp:
            return 0.0, start + i_tr + 1 + j_be, "BE"
        if j_tp < 10 ** 9:
            return sgn * (tp - e), start + i_tr + 1 + j_tp, "TP"
        return eod_exit()

    if i_sl < 10 ** 9 and i_sl <= i_tp:
        return -risk, start + i_sl, "SL"
    if i_tp < 10 ** 9:
        return sgn * (tp - e), start + i_tp, "TP"
    return eod_exit()


def window_of(ts: pd.Timestamp, windows) -> str:
    t = ts.time()
    for k, (a, b) in enumerate(windows, 1):
        if dtime(*map(int, a.split(":"))) <= t <= dtime(*map(int, b.split(":"))):
            return f"W{k}"
    return "OUT"


def simulate(sig: pd.DataFrame, days: dict, windows, min_rr=3.0, allow=("W1", "W2"),
             mode="tp2", entry="market", fill_wait=30, cost=COST_PTS, fresh=None) -> pd.DataFrame:
    trades = []
    if sig is None or sig.empty:
        return pd.DataFrame()
    if fresh:                                    # (max sweep age, max CHoCH age) in minutes
        if "sweep_age" not in sig or "choch_age" not in sig:
            raise SystemExit("The cached signal scan is from before the freshness test — rerun with --rescan.")
        sig = sig[(sig["sweep_age"] <= fresh[0]) & (sig["choch_age"] <= fresh[1])]
    for day, g in sig.groupby("day", sort=True):
        D = days.get(day)
        if D is None:
            continue
        busy, used = -1, set()
        for s in g.sort_values("now").itertuples():
            ts = pd.Timestamp(s.now)
            win = window_of(ts, windows)
            if win not in allow or (s.rr or 0) < min_rr:
                continue
            i0 = D.idx(ts)
            zkey = (s.side, round(s.lo), round(s.hi))
            if i0 <= busy or zkey in used or i0 > D.eod:
                continue
            sgn = 1 if s.side == "BULLISH" else -1
            tp1 = None if pd.isna(s.tp1) else float(s.tp1)
            if entry == "limit":
                mid = (s.lo + s.hi) / 2
                fill = None
                for j in range(i0, min(i0 + fill_wait, D.eod + 1)):
                    if (D.h[j] >= s.tp) if sgn > 0 else (D.l[j] <= s.tp):
                        break                                           # ran to target without us
                    if (D.l[j] <= mid) if sgn > 0 else (D.h[j] >= mid):
                        fill = (j, min(mid, D.o[j]) if sgn > 0 else max(mid, D.o[j]))
                        break
                used.add(zkey)
                if fill is None:
                    continue
                start, e = fill
            else:
                start, e = i0, float(D.o[i0])
            risk, reward = sgn * (e - s.sl), sgn * (s.tp - e)
            if risk <= 0 or reward <= 0:
                used.add(zkey)
                continue
            res = _run(D, start, sgn, e, float(s.sl), float(s.tp), tp1, mode)
            if res is None:
                continue
            pnl, j_exit, outcome = res
            used.add(zkey)
            busy = j_exit
            trades.append(dict(day=day, time=ts.strftime("%H:%M"), window=win, side=s.side, zone=s.kind,
                               plan_rr=round(float(s.rr), 2), entry=round(e, 1), sl=round(float(s.sl), 1),
                               tp=round(float(s.tp), 1), tp1=tp1, risk_pts=round(risk, 1),
                               reward_pts=round(reward, 1), start_i=start, exit_time=D.t[j_exit].strftime("%H:%M"),
                               outcome=outcome, pnl_pts=round(pnl - cost, 1), R=round((pnl - cost) / risk, 3)))
    return pd.DataFrame(trades)


def _win_minutes(D: Day, win: str, windows) -> np.ndarray:
    tt = D.t.dt.time
    ok = np.array([(window_of(pd.Timestamp(x), windows) == win) and dtime(9, 31) <= x.time() <= LAST_ENTRY
                   for x in D.t])
    return np.where(ok)[0]


def nulls(tr: pd.DataFrame, days: dict, windows, mode: str, reps: int, seed=11, cost=COST_PTS) -> dict:
    """A: random minute (same day/window/side/geometry). B: same minute & entry, random side."""
    if tr.empty:
        return {}
    rng = np.random.default_rng(seed)
    real = float(tr["R"].mean())
    cache = {}
    a_means, b_means = np.empty(reps), np.empty(reps)
    rows = list(tr.itertuples())
    for r in range(reps):
        ra, rb = [], []
        for t in rows:
            D = days[t.day]
            sgn = 1 if t.side == "BULLISH" else -1
            d1 = None if (t.tp1 is None or pd.isna(t.tp1)) else abs(float(t.tp1) - t.entry)
            # A — random time
            key = (t.day, t.window)
            if key not in cache:
                cache[key] = _win_minutes(D, t.window, windows)
            mins = cache[key]
            if len(mins):
                j = int(rng.choice(mins))
                e = float(D.o[j])
                res = _run(D, j, sgn, e, e - sgn * t.risk_pts, e + sgn * t.reward_pts,
                           (e + sgn * d1) if d1 else None, mode)
                if res:
                    ra.append((res[0] - cost) / t.risk_pts)
            # B — random side at the same entry
            s2 = 1 if rng.random() < 0.5 else -1
            e = t.entry
            res = _run(D, int(t.start_i), s2, e, e - s2 * t.risk_pts, e + s2 * t.reward_pts,
                       (e + s2 * d1) if d1 else None, mode)
            if res:
                rb.append((res[0] - cost) / t.risk_pts)
        a_means[r] = np.mean(ra) if ra else np.nan
        b_means[r] = np.mean(rb) if rb else np.nan
    a_means, b_means = a_means[~np.isnan(a_means)], b_means[~np.isnan(b_means)]
    return dict(real=real,
                time_null=float(a_means.mean()), p_time=float(((a_means >= real).sum() + 1) / (len(a_means) + 1)),
                side_null=float(b_means.mean()), p_side=float(((b_means >= real).sum() + 1) / (len(b_means) + 1)))


def stats(tr: pd.DataFrame) -> dict:
    if tr.empty:
        return dict(n=0)
    days = sorted(tr["day"].unique())
    half = days[len(days) // 2] if len(days) > 1 else days[0]
    a, b = tr[tr["day"] < half], tr[tr["day"] >= half]
    win = (tr["R"] > 0).mean()
    wins, losses = tr.loc[tr["R"] > 0, "R"], tr.loc[tr["R"] <= 0, "R"]
    eq = tr["R"].cumsum()
    return dict(n=int(len(tr)), days=int(tr["day"].nunique()), win_rate=round(float(win), 3),
                avg_R=round(float(tr["R"].mean()), 3), total_R=round(float(tr["R"].sum()), 1),
                avg_win_R=round(float(wins.mean()), 2) if len(wins) else 0.0,
                avg_loss_R=round(float(losses.mean()), 2) if len(losses) else 0.0,
                max_dd_R=round(float((eq.cummax() - eq).max()), 1),
                half1_avg_R=round(float(a["R"].mean()), 3) if len(a) else None,
                half2_avg_R=round(float(b["R"].mean()), 3) if len(b) else None,
                buy_n=int((tr["side"] == "BULLISH").sum()), sell_n=int((tr["side"] == "BEARISH").sum()),
                outcomes=tr["outcome"].value_counts().to_dict())


# ═══════════════════════════════════════════════════════════════════════════
# Session-sweep check (opening range / first hour taken both sides?)
# ═══════════════════════════════════════════════════════════════════════════
def _sweep_one(h, l, c, n_open):
    hi, lo = h[:n_open].max(), l[:n_open].min()
    H, L = h[n_open:], l[n_open:]
    ih, il = _first(H > hi), _first(L < lo)
    both = ih < 10 ** 9 and il < 10 ** 9
    one = (ih < 10 ** 9) or (il < 10 ** 9)
    return both, one


def session_sweeps(m1: pd.DataFrame, reps=200, seed=5) -> dict:
    rng = np.random.default_rng(seed)
    out = {}
    for label, n_open in (("Opening range (first 15 min)", 15), ("First hour (IB)", 60)):
        real, null, ranges = [], [], []
        one_real, one_null = [], []
        for _, g in m1.groupby(m1["timestamp"].dt.date):
            h, l, c = g["high"].to_numpy(float), g["low"].to_numpy(float), g["close"].to_numpy(float)
            if len(c) < n_open + 60:
                continue
            both, one = _sweep_one(h, l, c, n_open)
            real.append(both); one_real.append(one)
            ranges.append(h[:n_open].max() - l[:n_open].min())
            # null: shuffle the rest-of-day bar moves (keeps the day's volatility, kills any session logic)
            dc_ = np.diff(c[n_open - 1:])
            up, dn = (h - c)[n_open:], (c - l)[n_open:]
            bs, os_ = [], []
            for _ in range(reps):
                p = rng.permutation(len(dc_))
                cc = c[n_open - 1] + np.cumsum(dc_[p])
                hh, ll = np.r_[h[:n_open], cc + up[p]], np.r_[l[:n_open], cc - dn[p]]
                b_, o_ = _sweep_one(hh, ll, None, n_open)
                bs.append(b_); os_.append(o_)
            null.append(np.mean(bs)); one_null.append(np.mean(os_))
        if not real:
            continue
        real, null, ranges = np.array(real, float), np.array(null, float), np.array(ranges)
        q = np.quantile(ranges, [1 / 3, 2 / 3])
        terc = {}
        for nm, m in (("small", ranges <= q[0]), ("medium", (ranges > q[0]) & (ranges <= q[1])), ("large", ranges > q[1])):
            terc[nm] = dict(n=int(m.sum()), range_pts=f"{ranges[m].min():.0f}–{ranges[m].max():.0f}",
                            both=round(float(real[m].mean()), 3), chance=round(float(null[m].mean()), 3))
        # p: day-level bootstrap of (real - null) difference around 0
        diff = real - null
        boot = np.array([rng.choice(diff, len(diff)).mean() for _ in range(2000)])
        out[label] = dict(days=int(len(real)), both=round(float(real.mean()), 3), chance=round(float(null.mean()), 3),
                          edge_pts=round(float(diff.mean() * 100), 1),
                          p=round(float(min(1.0, 2 * min((boot <= 0).mean(), (boot >= 0).mean()))), 4),
                          by_open_range=terc)
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Report
# ═══════════════════════════════════════════════════════════════════════════
def _row(name, s, nl=None):
    if not s.get("n"):
        return f"  {name:<28} no trades"
    t = (f"  {name:<28} n={s['n']:>4}  win {s['win_rate'] * 100:5.1f}%  avg {s['avg_R']:+.2f}R  "
         f"total {s['total_R']:+7.1f}R  maxDD {s['max_dd_R']:.1f}R")
    if nl:
        t += f"  | random-time {nl['time_null']:+.2f}R p={nl['p_time']:.3f}  random-side {nl['side_null']:+.2f}R p={nl['p_side']:.3f}"
    return t


def main():
    import runlog
    runlog.start("backtest_five_rule")
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", type=int, default=0, help="download N calendar days of 1m history from Dhan first")
    ap.add_argument("--reps", type=int, default=500)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--step", type=int, default=1, help="evaluate every N minutes (1 = every minute)")
    ap.add_argument("--rescan", action="store_true", help="ignore the cached signal scan")
    ap.add_argument("--before", help="score only sessions BEFORE this date (YYYY-MM-DD) — out-of-sample check")
    ap.add_argument("--after", help="score only sessions ON/AFTER this date (YYYY-MM-DD)")
    ap.add_argument("--entry", choices=("market", "limit"), default="market",
                    help="entry style for the HEADLINE (fix it BEFORE looking at results)")
    ap.add_argument("--tag", default="", help="suffix for output files, e.g. oos → five_rule_oos.json")
    ap.add_argument("--fresh-sweep", type=float, default=90, help="freshness test: max minutes since the liquidation sweep")
    ap.add_argument("--fresh-choch", type=float, default=30, help="freshness test: max minutes since the 1m CHoCH")
    a = ap.parse_args()
    after = datetime.strptime(a.after, "%Y-%m-%d").date() if a.after else None
    before = datetime.strptime(a.before, "%Y-%m-%d").date() if a.before else None
    sfx = f"_{a.tag}" if a.tag else ""
    sig_cache = OUT_DIR / f"five_rule_signals{sfx}.csv"

    if a.fetch:
        print(f"Downloading ~{a.fetch} days of 1m NIFTY candles from Dhan …")
        print(f"  saved {fetch_history(a.fetch)} sessions to {HIST_DIR}")

    m1 = load_1m()
    n_days = m1["timestamp"].dt.date.nunique() if len(m1) else 0
    all_days = sorted(m1["timestamp"].dt.date.unique()) if len(m1) else []
    scored = [d for i, d in enumerate(all_days) if i >= WARMUP_DAYS and in_range(d, after, before)]
    if (after or before) and len(scored) < 20:
        sys.exit(f"Only {len(scored)} sessions fall in the requested range "
                 f"({a.after or 'start'} → {a.before or 'end'}). Fetch more history first, e.g. --fetch 730.")
    if n_days < WARMUP_DAYS + 10:
        sys.exit(f"Only {n_days} usable 1m sessions found in {', '.join(map(str, candle_dirs()))}.\n"
                 f"Run:  .venv/bin/python backtest_five_rule.py --fetch 365   (or backfill_dhan_history.py)")
    print(f"Loaded {n_days} sessions of 1m NIFTY: {m1['timestamp'].iloc[0]:%Y-%m-%d} → {m1['timestamp'].iloc[-1]:%Y-%m-%d}")

    import smc_five_rule as fr
    rules = fr._rules()
    windows, min_rr = rules["windows"], float(rules["min_rr"])
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    key = f"{n_days}|{m1['timestamp'].iloc[0]}|{m1['timestamp'].iloc[-1]}|{a.step}|{after}|{before}"
    sig = None
    if sig_cache.exists() and not a.rescan:
        try:
            cached = pd.read_csv(sig_cache)
            if "sweep_age" not in cached:                 # scan made before the freshness test → rescan
                raise ValueError("old cache")
            if (sig_cache.with_suffix(".key").read_text().strip() == key):
                sig = cached
                print(f"Using cached signal scan ({len(sig)} TAKE-minutes). Add --rescan after changing rules.")
        except Exception:
            sig = None
    if sig is None:
        sig = scan_all(m1, a.step, a.workers, after, before)
        sig.to_csv(sig_cache, index=False)
        sig_cache.with_suffix(".key").write_text(key)
    days = {str(k): Day(g.reset_index(drop=True)) for k, g in m1.groupby(m1["timestamp"].dt.date)}

    res = dict(generated=datetime.now().isoformat(timespec="seconds"), sessions=n_days,
               scored_sessions=len(scored), scored_from=str(scored[0]) if scored else None,
               scored_to=str(scored[-1]) if scored else None, take_minutes=int(len(sig)), cost_pts=COST_PTS,
               windows=windows, min_rr=min_rr, headline_entry=a.entry, tag=a.tag)
    print("\n" + "═" * 100)
    if after or before:
        print(f"OUT-OF-SAMPLE RANGE: scoring {len(scored)} sessions {scored[0]} → {scored[-1]} only")
    print(f"5-RULE SMC MODEL · walk-forward on {len(scored)} sessions · cost {COST_PTS} pt/trade · "
          f"SL wins ties · flat 15:15 · {a.reps} random shuffles")
    print("═" * 100)

    head = simulate(sig, days, windows, min_rr, ("W1", "W2"), "tp2", a.entry)
    hs, hn = stats(head), nulls(head, days, windows, "tp2", a.reps)
    res["headline"] = dict(stats=hs, null=hn)
    ent_txt = "market entry at next 1m open" if a.entry == "market" else "LIMIT entry at zone middle (30 min to fill)"
    print(f"\nHEADLINE (R:R ≥ {min_rr:g}, windows W1+W2, {ent_txt}, full TP2)")
    print(_row("5-Rule", hs, hn))
    if hs.get("n"):
        print(f"  avg win {hs['avg_win_R']:+.2f}R · avg loss {hs['avg_loss_R']:+.2f}R · halves "
              f"{hs['half1_avg_R']:+.2f}R / {hs['half2_avg_R']:+.2f}R · BUY {hs['buy_n']} / SELL {hs['sell_n']} · {hs['outcomes']}")

    print("\nWHICH TIME WINDOW? (R:R ≥ %g, market, full TP2)" % min_rr)
    res["windows_test"] = {}
    for nm, allow in (("W1 " + "–".join(windows[0]), ("W1",)),
                      ("W2 " + "–".join(windows[1]) if len(windows) > 1 else "W2", ("W2",)),
                      ("Outside windows", ("OUT",))):
        tr = simulate(sig, days, windows, min_rr, allow, "tp2", "market")
        s_, n_ = stats(tr), nulls(tr, days, windows, "tp2", max(100, a.reps // 2))
        res["windows_test"][nm] = dict(stats=s_, null=n_)
        print(_row(nm, s_, n_))

    print("\nMINIMUM R:R (W1+W2, market, full TP2)")
    res["rr_test"] = {}
    for th in (1.0, 1.5, 2.0, 3.0, 4.0):
        tr = simulate(sig, days, windows, th, ("W1", "W2"), "tp2", "market")
        s_, n_ = stats(tr), nulls(tr, days, windows, "tp2", max(100, a.reps // 2))
        res["rr_test"][str(th)] = dict(stats=s_, null=n_)
        print(_row(f"R:R ≥ {th:g}", s_, n_))

    print("\nEXIT STYLE (R:R ≥ %g, W1+W2, market)" % min_rr)
    res["exit_test"] = {}
    for nm, mode in (("Full TP2 (external swing)", "tp2"), ("Breakeven at +1R", "be1r"),
                     ("50% at TP1, rest BE→TP2", "half_tp1")):
        tr = simulate(sig, days, windows, min_rr, ("W1", "W2"), mode, "market")
        s_, n_ = stats(tr), nulls(tr, days, windows, mode, max(100, a.reps // 2))
        res["exit_test"][nm] = dict(stats=s_, null=n_)
        print(_row(nm, s_, n_))

    print("\nENTRY STYLE (R:R ≥ %g, W1+W2, full TP2)" % min_rr)
    res["entry_test"] = {}
    for nm, ent in (("Market at next 1m open", "market"), ("Limit at zone middle (30m)", "limit")):
        tr = simulate(sig, days, windows, min_rr, ("W1", "W2"), "tp2", ent)
        s_, n_ = stats(tr), nulls(tr, days, windows, "tp2", max(100, a.reps // 2))
        res["entry_test"][nm] = dict(stats=s_, null=n_)
        print(_row(nm, s_, n_))
        if ent == "limit":
            tr.to_csv(OUT_DIR / f"five_rule{sfx}_trades_limit.csv", index=False)
    head.to_csv(OUT_DIR / f"five_rule{sfx}_trades.csv", index=False)

    fr_cfg = (float(a.fresh_sweep), float(a.fresh_choch))
    print(f"\nFRESHNESS TEST — fixed BEFORE the run: liquidation sweep ≤ {fr_cfg[0]:g} min old AND 1m CHoCH ≤ {fr_cfg[1]:g} min old")
    res["fresh_test"] = {"sweep_max_min": fr_cfg[0], "choch_max_min": fr_cfg[1]}
    for nm, ent in (("Fresh · " + ("market" if a.entry == "market" else "limit") + " (headline entry)", a.entry),
                    ("Fresh · " + ("limit" if a.entry == "market" else "market"), "limit" if a.entry == "market" else "market")):
        tr = simulate(sig, days, windows, min_rr, ("W1", "W2"), "tp2", ent, fresh=fr_cfg)
        s_, n_ = stats(tr), nulls(tr, days, windows, "tp2", max(100, a.reps // 2))
        res["fresh_test"][nm] = dict(stats=s_, null=n_)
        print(_row(nm, s_, n_))
        if s_.get("n"):
            print(f"      halves {s_['half1_avg_R']:+.2f}R / {s_['half2_avg_R']:+.2f}R · BUY {s_['buy_n']} / SELL {s_['sell_n']}")
    hf = res["fresh_test"][next(k for k in res["fresh_test"] if k.endswith("(headline entry)"))]
    fs_, fn_ = hf["stats"], hf["null"]
    fok = (fs_.get("n", 0) >= 30 and fs_.get("avg_R", 0) > 0 and fn_.get("p_time", 1) < 0.05 and fn_.get("p_side", 1) < 0.05
           and (fs_.get("half1_avg_R") or 0) > 0 and (fs_.get("half2_avg_R") or 0) > 0)
    res["fresh_test"]["passed"] = bool(fok)
    print("  → " + ("✔ freshness rule PASSES — worth switching on (fresh_sweep_max_min / fresh_choch_max_min in the rules file)"
                    if fok else "✘ freshness rule does not prove an edge either — leave it off"))

    print("\nSESSION-SWEEP CHECK — does NIFTY take BOTH sides of the opening range / first hour more than chance?")
    sw = session_sweeps(m1[m1["timestamp"].dt.date.isin(set(scored))] if (after or before) else m1)
    res["session_sweeps"] = sw
    for nm, v in sw.items():
        print(f"  {nm:<30} both sides taken {v['both'] * 100:5.1f}% of {v['days']} days · shuffled-day chance "
              f"{v['chance'] * 100:5.1f}% · edge {v['edge_pts']:+.1f} pts · p={v['p']:.3f}")
        for t_, x in v["by_open_range"].items():
            print(f"      {t_:<6} range {x['range_pts']:>9} pts  both {x['both'] * 100:5.1f}% vs chance {x['chance'] * 100:5.1f}%  (n={x['n']})")

    # verdict
    ok = (hs.get("n", 0) >= 30 and hs.get("avg_R", 0) > 0 and hn.get("p_time", 1) < 0.05
          and hn.get("p_side", 1) < 0.05 and (hs.get("half1_avg_R") or 0) > 0 and (hs.get("half2_avg_R") or 0) > 0)
    res["passed"] = bool(ok)
    print("\nVERDICT")
    if hs.get("n", 0) < 30:
        print(f"  ⚠ Only {hs.get('n', 0)} trades — too few to judge. Fetch more history (--fetch 730) and rerun.")
    elif ok:
        print("  ✔ EDGE: the 5-Rule model beat BOTH random controls and was positive in both halves of the data.")
        print("    Step 2 upgrades can be added and each one re-tested against this baseline.")
    else:
        why = []
        if hs.get("avg_R", 0) <= 0:
            why.append("average R is not positive after cost")
        if hn.get("p_time", 1) >= 0.05:
            why.append(f"random entry times did about as well (p={hn.get('p_time', 1):.2f})")
        if hn.get("p_side", 1) >= 0.05:
            why.append(f"coin-flip direction did about as well (p={hn.get('p_side', 1):.2f})")
        if (hs.get("half1_avg_R") or 0) <= 0 or (hs.get("half2_avg_R") or 0) <= 0:
            why.append("not positive in both halves")
        print("  ✘ NO PROVEN EDGE YET: " + "; ".join(why) + ".")
        print("    Treat the 5-Rule panel as a checklist, not a signal. Don't build Step 2 on it yet.")
    print("  Note: the best row in any table above will look good partly by luck (many rows tested).")
    print("        Trust the HEADLINE, which uses the settings fixed before the test.")
    (OUT_DIR / f"five_rule{sfx}.json").write_text(json.dumps(res, indent=2, default=str))
    print(f"\nSaved: {OUT_DIR / f'five_rule{sfx}.json'} · trades: {OUT_DIR / f'five_rule{sfx}_trades.csv'}")
    if a.tag:
        print("  (tagged run — the live 5-Rule panels still read five_rule.json)")
    print("Information only — not financial advice.")


if __name__ == "__main__":
    main()
