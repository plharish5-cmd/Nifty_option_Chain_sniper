"""
garch_range.py — GARCH(1,1) expected-move box for NIFTY 50, with an honest test on YOUR data.

What it gives you
  • Today's box (made from yesterday's close): 1σ and 2σ ranges in NIFTY points. Price outside the 1σ box is a
    stretched day (about 1 in 3 days by design); outside 2σ is rare (about 1 in 20).
  • Next session's box (after the close), and a 5-day box for swing / weekly options.
  • Half-life of a volatility shock (how fast a big day's extra volatility fades).

What GARCH is (Engle 1982 ARCH — Nobel 2003; Bollerslev 1986 GARCH)
  tomorrow's variance = ω + α·(today's surprise)² + β·(today's variance)
  α = how much a big day expands tomorrow's range (volatility clustering), β = how slowly it fades,
  half-life = ln(0.5) / ln(α + β). It forecasts the SIZE of the move, never the direction.

Honest test (run it):  .venv/bin/python garch_range.py --test
  Walk-forward: refits every 21 sessions on past data only, forecasts each next day, then checks how often the
  close (and the whole day's high–low) stayed inside the 1σ / 2σ box. Compared with two simple baselines:
  20-day historical volatility and RiskMetrics EWMA (λ = 0.94). Scored with QLIKE (the standard loss for
  volatility forecasts) and a paired t-test, so "GARCH is better" has to be earned, not assumed.
  Writes data/backtests/garch.json. Other uses:
     .venv/bin/python garch_range.py               # print today's / next session's box
Reads stored files only (no web calls). Information only — not financial advice.
"""
from __future__ import annotations

import json
import math
import sys
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "data" / "backtests" / "garch.json"
Z1, Z2 = 1.0, 2.0


# ═══════════════════════════════════════════════════════════════════════════
# Data (offline only)
# ═══════════════════════════════════════════════════════════════════════════
def _norm(d: pd.DataFrame) -> pd.DataFrame:
    d = d.copy()
    if not any(str(c).lower() in ("day", "date", "timestamp") for c in d.columns):
        d = d.reset_index()
    d.columns = [str(c).strip().lower() for c in d.columns]
    dcol = next(c for c in ("day", "date", "timestamp", "index") if c in d.columns)
    d["day"] = pd.to_datetime(d[dcol], errors="coerce")
    try:
        d["day"] = d["day"].dt.tz_localize(None)
    except (TypeError, AttributeError):
        pass
    d["day"] = d["day"].dt.normalize()
    for c in ("open", "high", "low", "close"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d.dropna(subset=["day", "open", "high", "low", "close"])
    d = d[(d["high"] >= d["low"]) & (d["close"] > 0)]
    return d.sort_values("day").drop_duplicates("day", keep="last")[["day", "open", "high", "low", "close"]]


def _from_1m() -> pd.DataFrame:
    parts = []
    for folder in (ROOT / "data" / "candles_hist", ROOT / "data" / "candles"):
        for f in sorted(folder.glob("20*.csv")) if folder.exists() else []:
            try:
                x = pd.read_csv(f, usecols=lambda c: c.lower() in ("timestamp", "open", "high", "low", "close"))
                x.columns = [c.lower() for c in x.columns]
                if len(x) < 300:
                    continue
                parts.append(dict(day=pd.Timestamp(f.stem[:10]), open=float(x["open"].iloc[0]), high=float(x["high"].max()),
                                  low=float(x["low"].min()), close=float(x["close"].iloc[-1])))
            except Exception:
                continue
    return pd.DataFrame(parts)


@lru_cache(maxsize=4)
def _load_cached(_stamp: str) -> pd.DataFrame:
    frames = []
    cache = ROOT / "data" / "ohlc" / "nifty_daily_backtest_cache.csv"
    if cache.exists():
        try:
            frames.append(_norm(pd.read_csv(cache)))
        except Exception:
            pass
    try:
        import levels
        frames.append(_norm(levels.load_ohlc()))
    except Exception:
        pass
    m = _from_1m()
    if len(m):
        frames.append(_norm(m))                      # recent days from 1-min files win (most up to date)
    if not frames:
        return pd.DataFrame(columns=["day", "open", "high", "low", "close"])
    d = pd.concat(frames, ignore_index=True).sort_values("day").drop_duplicates("day", keep="last")
    return d.reset_index(drop=True)


def load_daily() -> pd.DataFrame:
    return _load_cached(datetime.now().strftime("%Y-%m-%d %H"))


# ═══════════════════════════════════════════════════════════════════════════
# GARCH(1,1), normal errors, fitted by maximum likelihood (scipy only)
# ═══════════════════════════════════════════════════════════════════════════
def _sig(x):
    return 1.0 / (1.0 + np.exp(-x))


def _unpack(th, var):
    mu = th[0]
    omega = math.exp(th[1]) * var * 0.01
    pers = 0.9995 * _sig(th[2])
    alpha = pers * _sig(th[3])
    return mu, omega, alpha, pers - alpha


def _filter(r, mu, omega, alpha, beta):
    """s2[t] = variance for day t; s2[-1] = the forecast for the next day. Vectorised recursion (lfilter)."""
    from scipy.signal import lfilter
    e = np.asarray(r, float) - mu
    s0 = float(np.var(r))
    x = omega + alpha * e ** 2
    y, _ = lfilter([1.0], [1.0, -beta], x, zi=np.array([beta * s0]))
    return np.r_[s0, y]


def fit(r: np.ndarray) -> dict:
    from scipy.optimize import minimize
    r = np.asarray(r, float)
    var = float(np.var(r))

    def nll(th):
        mu, om, a, b = _unpack(th, var)
        s2 = _filter(r, mu, om, a, b)[:-1]
        s2 = np.maximum(s2, 1e-10)
        return 0.5 * np.sum(np.log(s2) + (r - mu) ** 2 / s2)
    x0 = np.array([float(np.mean(r)), math.log(5.0), 2.9, -2.2])          # ~α 0.09, β 0.89
    res = minimize(nll, x0, method="Nelder-Mead", options=dict(maxiter=4000, xatol=1e-6, fatol=1e-6))
    mu, om, a, b = _unpack(res.x, var)
    pers = a + b
    return dict(mu=mu, omega=om, alpha=a, beta=b, persistence=pers,
                long_run_daily=math.sqrt(om / (1 - pers)) if pers < 1 else float("nan"),
                half_life=(math.log(0.5) / math.log(pers)) if 0 < pers < 1 else float("inf"),
                next_sigma=float(math.sqrt(_filter(r, mu, om, a, b)[-1])), n=len(r), ok=bool(res.success))


def horizon_sigma(p: dict, sigma1: float, h: int) -> float:
    """σ of the h-day cumulative return (sum of per-day forecast variances)."""
    vl = p["long_run_daily"] ** 2
    tot = sum(vl + p["persistence"] ** (k) * (sigma1 ** 2 - vl) for k in range(h))
    return math.sqrt(tot)


def _returns(d: pd.DataFrame) -> pd.Series:
    """Daily log returns in %. A |move| above 15% is treated as a data splice/error, not a market move
    (NIFTY's worst day since 1990s is about −13%), and set to NaN so it cannot blow up the volatility."""
    r = 100 * np.log(d["close"]).diff()
    return r.where(r.abs() < 15)


@lru_cache(maxsize=64)
def _box_cached(day_iso: str, stamp: str) -> dict | None:
    d = load_daily()
    past = d[d["day"] < pd.Timestamp(day_iso)]
    if len(past) < 300:
        return None
    r = _returns(past).dropna().to_numpy()[-1500:]
    if len(r) < 250:
        return None
    p = fit(r)
    s = p["next_sigma"]
    c0 = float(past["close"].iloc[-1])
    pts1, pts2 = c0 * s / 100 * Z1, c0 * s / 100 * Z2
    s5 = horizon_sigma(p, s, 5)
    return dict(day=day_iso, ref_day=f"{past['day'].iloc[-1]:%Y-%m-%d}", ref_close=c0, sigma_pct=s, pts1=pts1, pts2=pts2,
                lo1=c0 - pts1, hi1=c0 + pts1, lo2=c0 - pts2, hi2=c0 + pts2, week_pts1=c0 * s5 / 100,
                half_life=p["half_life"], alpha=p["alpha"], beta=p["beta"],
                long_run_pct=p["long_run_daily"], ann_vol=s * math.sqrt(252))


def box_for(day) -> dict | None:
    """The GARCH box for session `day` (date / 'YYYY-MM-DD'), using only closes BEFORE that day."""
    iso = f"{pd.Timestamp(day):%Y-%m-%d}"
    return _box_cached(iso, datetime.now().strftime("%Y-%m-%d %H"))


def validation() -> dict:
    try:
        return json.loads(OUT.read_text())
    except Exception:
        return {}


def text_line(day, spot: float | None) -> str:
    b = box_for(day)
    if b is None:
        return "GARCH RANGE: N/A — needs ≥300 daily NIFTY closes (run backtest_daily_bias.py once to cache them)."
    v = validation()
    cov = v.get("garch", {})
    tag = (f" · on your data the close stayed inside 1σ {cov['cover1_close'] * 100:.0f}% / 2σ {cov['cover2_close'] * 100:.0f}% of days"
           if cov else " · not yet tested on your data (run garch_range.py --test)")
    L = [f"GARCH RANGE (size of the move, not direction) · from {b['ref_day']} close {b['ref_close']:,.0f} · σ {b['sigma_pct']:.2f}%"
         f" ({b['ann_vol']:.0f}% annualised) · vol half-life {b['half_life']:.0f} days",
         f"  1σ box {b['lo1']:,.0f} – {b['hi1']:,.0f} (±{b['pts1']:,.0f} pts) · 2σ box {b['lo2']:,.0f} – {b['hi2']:,.0f} "
         f"(±{b['pts2']:,.0f}) · 5-day 1σ ±{b['week_pts1']:,.0f}" + tag]
    if spot:
        mv = spot - b["ref_close"]
        z = mv / (b["pts1"] or 1)
        t1 = (f"intraday price leaves the 1σ box on ~{(1 - cov['cover1_range']) * 100:.0f}% of days, but CLOSES outside it on only "
              f"~{(1 - cov['cover1_close']) * 100:.0f}%") if cov.get("cover1_range") is not None else "about 1 day in 3"
        t2 = (f"closes outside 2σ on only ~{(1 - cov['cover2_close']) * 100:.0f}% of days") if cov.get("cover2_close") is not None \
            else "rare"
        where = (f"OUTSIDE 2σ — extreme day ({t2}); chasing is risky, gaps and reversals are common here" if abs(z) >= 2 else
                 f"OUTSIDE 1σ — stretched ({t1}); the extra room left is smaller than usual" if abs(z) >= 1 else
                 "inside the 1σ box — a normal day so far")
        L.append(f"  Now {spot:,.0f} = {mv:+,.0f} pts ({z:+.1f}σ) from yesterday's close → {where}")
    return "\n".join(L)


# ═══════════════════════════════════════════════════════════════════════════
# Walk-forward test
# ═══════════════════════════════════════════════════════════════════════════
def walk_forward(d: pd.DataFrame, start: int = 500, refit_every: int = 21, window: int = 1500) -> pd.DataFrame:
    d = d.reset_index(drop=True)
    r = _returns(d).to_numpy()
    rows, p = [], None
    for t in range(start, len(d)):
        if p is None or (t - start) % refit_every == 0:
            hist = r[max(1, t - window):t]
            p = fit(hist[~np.isnan(hist)])
        hist = r[max(1, t - window):t]
        hist = hist[~np.isnan(hist)]
        s_g = math.sqrt(_filter(hist, p["mu"], p["omega"], p["alpha"], p["beta"])[-1])
        s_h = float(np.std(hist[-20:], ddof=1))
        w = 0.94 ** np.arange(len(hist) - 1, -1, -1)
        s_e = math.sqrt(float(np.sum(w * (1 - 0.94) * hist ** 2)) / max(1e-12, float(np.sum(w * (1 - 0.94)))))
        c0 = float(d["close"].iloc[t - 1])
        if np.isnan(r[t]):
            continue
        rows.append(dict(day=d["day"].iloc[t], ret=r[t], mu=p["mu"], c0=c0, hi=float(d["high"].iloc[t]),
                         lo=float(d["low"].iloc[t]), garch=s_g, hv20=s_h, ewma=s_e))
    return pd.DataFrame(rows)


def _score(w: pd.DataFrame, col: str) -> dict:
    s = w[col].to_numpy()
    e = w["ret"].to_numpy() - (w["mu"].to_numpy() if col == "garch" else 0.0)
    z = np.abs(e) / s
    up = 100 * np.log(w["hi"] / w["c0"]).to_numpy()
    dn = -100 * np.log(w["lo"] / w["c0"]).to_numpy()
    hl1 = (up <= s) & (dn <= s)
    hl2 = (up <= 2 * s) & (dn <= 2 * s)
    q = np.log(s ** 2) + w["ret"].to_numpy() ** 2 / s ** 2
    return dict(cover1_close=float((z <= 1).mean()), cover2_close=float((z <= 2).mean()),
                cover1_range=float(hl1.mean()), cover2_range=float(hl2.mean()),
                qlike=float(q.mean()), _q=q)


def run_test():
    import runlog
    runlog.start("garch_range_test")
    d = load_daily()
    print(f"Daily NIFTY closes available: {len(d)}" + (f" ({d['day'].iloc[0]:%Y-%m-%d} → {d['day'].iloc[-1]:%Y-%m-%d})" if len(d) else ""))
    if len(d) < 800:
        sys.exit("Need ≥800 daily closes for an honest walk-forward test. Run backtest_daily_bias.py once (it caches "
                 "~7 years from Dhan to data/ohlc/nifty_daily_backtest_cache.csv), then rerun.")
    w = walk_forward(d)
    print(f"Walk-forward: {len(w)} out-of-sample days ({w['day'].iloc[0]:%Y-%m-%d} → {w['day'].iloc[-1]:%Y-%m-%d}), "
          f"refit every 21 sessions on past data only\n")
    res = {}
    print(f"{'model':<26}{'close in 1σ':>12}{'close in 2σ':>12}{'H–L in 1σ':>11}{'H–L in 2σ':>11}{'QLIKE':>9}")
    print(f"{'(normal-curve target)':<26}{'68.3%':>12}{'95.4%':>12}{'—':>11}{'—':>11}{'lower=better':>13}")
    for col, nm in (("garch", "GARCH(1,1)"), ("ewma", "EWMA λ=0.94 (RiskMetrics)"), ("hv20", "20-day historical vol")):
        s = _score(w, col)
        res[col] = {k: v for k, v in s.items() if not k.startswith("_")}
        res[col]["_q"] = s["_q"]
        print(f"{nm:<26}{s['cover1_close'] * 100:>11.1f}%{s['cover2_close'] * 100:>11.1f}%{s['cover1_range'] * 100:>10.1f}%"
              f"{s['cover2_range'] * 100:>10.1f}%{s['qlike']:>9.3f}")
    print("\nIs GARCH really better? (paired test on daily QLIKE loss; negative = GARCH better)")
    for base in ("ewma", "hv20"):
        dlt = res["garch"]["_q"] - res[base]["_q"]
        t = float(dlt.mean() / (dlt.std(ddof=1) / math.sqrt(len(dlt))))
        res[f"vs_{base}"] = dict(mean_diff=float(dlt.mean()), t=t, better=bool(t < -2))
        print(f"  GARCH vs {base:<5}: mean diff {dlt.mean():+.4f} · t = {t:+.2f} → "
              + ("GARCH clearly better" if t < -2 else "GARCH clearly WORSE" if t > 2 else "no clear difference"))
    # calm vs stormy
    hi_vol = w["garch"] > w["garch"].median()
    for nm, m in (("calm half", ~hi_vol), ("volatile half", hi_vol)):
        s = _score(w[m], "garch")
        print(f"  GARCH coverage, {nm:<13}: close in 1σ {s['cover1_close'] * 100:.0f}% · 2σ {s['cover2_close'] * 100:.0f}%")
    big = w.assign(z=np.abs(w["ret"] - w["mu"]) / w["garch"]).sort_values("z", ascending=False).head(5)
    print("  Worst misses (days the box failed): " + ", ".join(f"{r.day:%Y-%m-%d} {r.ret:+.1f}% ({r.z:.1f}σ)" for r in big.itertuples()))
    for k in res:
        if isinstance(res[k], dict):
            res[k].pop("_q", None)
    p = fit(_returns(d).dropna().to_numpy()[-1500:])
    res["latest_fit"] = {k: (float(v) if isinstance(v, (int, float, np.floating)) else v) for k, v in p.items()}
    res["generated"] = datetime.now().isoformat(timespec="seconds")
    res["days"] = int(len(w))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, indent=2, default=str))
    print(f"\nCurrent fit: α {p['alpha']:.3f} · β {p['beta']:.3f} · persistence {p['persistence']:.3f} · "
          f"half-life {p['half_life']:.0f} days · long-run σ {p['long_run_daily']:.2f}%/day")
    print("Reading it: well calibrated = close-in-1σ near 68% and in-2σ near 95%. Fat tails mean 2σ is usually a bit"
          " below 95% — that gap is the risk the box does NOT cover (gaps, news, wars).")
    print(f"Saved {OUT}. Information only — not financial advice.")


if __name__ == "__main__":
    if "--test" in sys.argv:
        run_test()
    else:
        today = date.today()
        print(text_line(today, None))
        nxt = today + timedelta(days=1)
        b = box_for(nxt)
        if b and b["ref_day"] != (box_for(today) or {}).get("ref_day"):
            print(f"\nNext session box (from {b['ref_day']} close {b['ref_close']:,.0f}): 1σ {b['lo1']:,.0f} – {b['hi1']:,.0f} · "
                  f"2σ {b['lo2']:,.0f} – {b['hi2']:,.0f}")
