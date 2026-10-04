"""
backtest_daily_bias.py — does YESTERDAY's daily candle predict TODAY on NIFTY?

The SMC course claim (forex, 15 yrs): "daily bias is right ~67% of the time".
This tests the same rule on YOUR NIFTY data, against honest baselines.

THE RULE (day t compared with day t-1's high/low; prediction is for day t+1)
  close above PDH                          → BULLISH
  wick above PDH, close back inside        → BEARISH  (sweep / rejection)
  wick below PDL, close back inside        → BULLISH  (sweep / rejection)
  close below PDL                          → BEARISH
  inside day, or swept BOTH sides          → NO READ

WHAT "RIGHT" MEANS (all three reported — pick the one you trade)
  dir      : day t+1 close > day t close (bullish) / < (bearish)
  candle   : day t+1 closes green (bullish) / red (bearish) vs its own open
  draw     : day t+1 trades beyond day t's high (bullish) / low (bearish)

BASELINES — a rule is only useful if it beats these
  chance level  : keep today's candle + signal, SHUFFLE tomorrow's move (from today's close)
                  across days, 2,000 times → what the rule scores with NO real link → p-value.
                  This matters: on PURE RANDOM prices the "draw" definition already scores ~67%
                  (a close near the high makes "tomorrow trades above today's high" likely by
                  mechanics alone) — the same number the course quotes. Only beating chance counts.
  always-long   : NIFTY drifts up; "bullish every day" is a real competitor (shown for dir)

Run:
  .venv/bin/python backtest_daily_bias.py                       # auto: data/ohlc → Dhan daily → cache
  .venv/bin/python backtest_daily_bias.py path/to/daily.csv
Writes data/backtests/daily_bias.json — the 5-Rule model reads it and only treats the
daily bias as a signal if it PASSED here.  Information only — not financial advice.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
OUT = ROOT / "data" / "backtests" / "daily_bias.json"
MIN_DAYS = 250
N_SHUFFLE = 2000
PASS_EDGE_PTS = 3.0       # must beat base rate by ≥3 percentage points …
PASS_P = 0.05             # … with shuffled-control p < 0.05 …
PASS_HALVES = True        # … and hold in BOTH halves of the history


def _norm(d: pd.DataFrame) -> pd.DataFrame:
    d = d.copy()
    if not any(c.lower() in ("day", "date", "timestamp") for c in d.columns):
        d = d.reset_index()
    d.columns = [str(c).strip().lower() for c in d.columns]
    dcol = next(c for c in ("day", "date", "timestamp", "index") if c in d.columns)
    d["day"] = pd.to_datetime(d[dcol], errors="coerce").dt.tz_localize(None).dt.normalize()
    for c in ("open", "high", "low", "close"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d.dropna(subset=["day", "open", "high", "low", "close"])
    d = d[(d["high"] >= d["low"]) & (d["close"] > 0)]
    return d.sort_values("day").drop_duplicates("day", keep="last")[["day", "open", "high", "low", "close"]].reset_index(drop=True)


def _usable(d) -> bool:
    return d is not None and len(d) >= MIN_DAYS and d["close"].nunique() >= 50


def load(path: Path | None = None) -> pd.DataFrame:
    """
    Daily NIFTY OHLC, first source that is real and long enough:
      1. a CSV path you pass on the command line
      2. your app's OHLC folder (levels.load_ohlc → data/ohlc/*.csv + _archive_master.csv)
      3. Dhan daily history (dhan_client.daily, uses the token in .env) — cached to
         data/ohlc/nifty_daily_backtest_cache.csv so the next run is offline
    """
    tried = []
    if path:
        d = _norm(pd.read_csv(path))
        if _usable(d):
            return d
        tried.append(f"{path}: {len(d)} days")
    try:
        import levels
        d = _norm(levels.load_ohlc())
        if _usable(d):
            print(f"Source: data/ohlc via levels.load_ohlc ({len(d)} days)")
            return d
        tried.append(f"data/ohlc: {len(d)} usable days")
    except Exception as e:                                   # noqa: BLE001
        tried.append(f"data/ohlc: {e}")
    cache = ROOT / "data" / "ohlc" / "nifty_daily_backtest_cache.csv"
    try:
        import dhan_client
        best = None
        for days in (3650, 2500, 1500, 800):
            try:
                raw = dhan_client.daily(days_back=days)
                d = _norm(raw)
                if best is None or len(d) > len(best):
                    best = d
                if _usable(d):
                    break
            except Exception as e:                           # noqa: BLE001
                tried.append(f"Dhan daily {days}d: {e}")
        if _usable(best):
            cache.parent.mkdir(parents=True, exist_ok=True)
            best.to_csv(cache, index=False)
            print(f"Source: Dhan daily history ({len(best)} days) → cached to {cache}")
            return best
        if best is not None:
            tried.append(f"Dhan daily: {len(best)} days")
    except Exception as e:                                   # noqa: BLE001
        tried.append(f"Dhan: {e}")
    if cache.exists():
        d = _norm(pd.read_csv(cache))
        if _usable(d):
            print(f"Source: cache {cache} ({len(d)} days)")
            return d
    raise SystemExit("No usable daily NIFTY OHLC found (need ≥ %d real days). Tried:\n  - " % MIN_DAYS
                     + "\n  - ".join(tried)
                     + "\nFix: refresh the Dhan token, or put a daily NIFTY CSV (date,open,high,low,close) in data/ohlc "
                       "and run:  .venv/bin/python backtest_daily_bias.py data/ohlc/<file>.csv")


def classify(d: pd.DataFrame) -> pd.Series:
    ph, pl = d["high"].shift(1), d["low"].shift(1)
    h, l, c = d["high"], d["low"], d["close"]
    sig = pd.Series("NONE", index=d.index)
    up_sweep = (h > ph) & (c <= ph)
    dn_sweep = (l < pl) & (c >= pl)
    sig[c > ph] = "BULL"
    sig[c < pl] = "BEAR"
    sig[up_sweep & ~dn_sweep] = "BEAR"
    sig[dn_sweep & ~up_sweep] = "BULL"
    sig[ph.isna()] = "NONE"
    return sig


def _next_moves(d: pd.DataFrame) -> dict[str, np.ndarray]:
    """Tomorrow's moves MEASURED FROM TODAY'S CLOSE (so they can be shuffled across days)."""
    n = d.shift(-1)
    return {"ret": (n["close"] - d["close"]).to_numpy(), "up": (n["high"] - d["close"]).to_numpy(),
            "dn": (d["close"] - n["low"]).to_numpy(), "body": (n["close"] - n["open"]).to_numpy()}


def _ok(name: str, d: pd.DataFrame, m: dict) -> tuple[np.ndarray, np.ndarray]:
    """(bullish call right, bearish call right) given today's candle and tomorrow's moves m."""
    c, h, l = d["close"].to_numpy(), d["high"].to_numpy(), d["low"].to_numpy()
    if name == "dir":
        return m["ret"] > 0, m["ret"] < 0
    if name == "candle":
        return m["body"] > 0, m["body"] < 0
    return c + m["up"] > h, c - m["dn"] < l          # draw: beyond today's high / low


def _hits(sig: np.ndarray, bull_ok: np.ndarray, bear_ok: np.ndarray) -> tuple[int, int]:
    b, s = sig == "BULL", sig == "BEAR"
    return int(bull_ok[b].sum() + bear_ok[s].sum()), int(b.sum() + s.sum())


def evaluate(d: pd.DataFrame, seed: int = 7) -> dict:
    """
    NULL MODEL: keep today's candle (and the signal) fixed, shuffle TOMORROW's move
    (measured from today's close) across days. That removes any real predictive link
    but keeps the mechanical one — e.g. a close near the high makes 'tomorrow trades
    above today's high' likely on PURE RANDOM DATA (~67%). Only beating THIS counts.
    """
    sig = classify(d).to_numpy()
    mv = _next_moves(d)
    keep = ~np.isnan(mv["ret"])
    rng = np.random.default_rng(seed)
    res = {"days": int(len(d)), "from": f"{d['day'].iloc[0]:%Y-%m-%d}", "to": f"{d['day'].iloc[-1]:%Y-%m-%d}",
           "signals": {k: int((sig[keep] == k).sum()) for k in ("BULL", "BEAR", "NONE")}, "outcomes": {},
           "null_model": "tomorrow's move shuffled across days, today's candle kept"}
    dk = d[keep].reset_index(drop=True)
    sk = sig[keep]
    mk = {k: v[keep] for k, v in mv.items()}
    idx = np.arange(len(dk))
    for name in ("dir", "candle", "draw"):
        bt_v, st_v = _ok(name, dk, mk)
        hit, n = _hits(sk, bt_v, st_v)
        acc = hit / n if n else float("nan")
        shuf = np.empty(N_SHUFFLE)
        shuf_b = np.empty(N_SHUFFLE)
        shuf_s = np.empty(N_SHUFFLE)
        mb, ms = sk == "BULL", sk == "BEAR"
        for i in range(N_SHUFFLE):
            pm = rng.permutation(idx)
            b2, s2 = _ok(name, dk, {k: v[pm] for k, v in mk.items()})
            shuf[i] = _hits(sk, b2, s2)[0] / n
            shuf_b[i] = b2[mb].mean() if mb.any() else np.nan
            shuf_s[i] = s2[ms].mean() if ms.any() else np.nan
        base = float(shuf.mean())
        pval = float((np.sum(shuf >= acc) + 1) / (N_SHUFFLE + 1))
        se = math.sqrt(acc * (1 - acc) / n) if n else float("nan")
        half = len(sk) // 2
        halves = []
        for a_, b_ in ((0, half), (half, len(sk))):
            sub = dk.iloc[a_:b_].reset_index(drop=True)
            subm = {k: v[a_:b_] for k, v in mk.items()}
            h3, n3 = _hits(sk[a_:b_], *_ok(name, sub, subm))
            sh3 = []
            for _ in range(300):
                pm = rng.permutation(len(sub))
                sh3.append(_hits(sk[a_:b_], *_ok(name, sub, {k: v[pm] for k, v in subm.items()}))[0] / n3)
            halves.append({"acc": h3 / n3 if n3 else float("nan"), "base": float(np.mean(sh3)), "n": n3})
        by_type = {}
        for tname in ("BULL", "BEAR"):
            msk = sk == tname
            ok = (bt_v if tname == "BULL" else st_v)[msk]
            ch = shuf_b if tname == "BULL" else shuf_s
            by_type[tname] = {"n": int(msk.sum()), "acc": float(ok.mean()) if msk.any() else float("nan"),
                              "chance": float(np.nanmean(ch)),
                              "p": float((np.sum(ch >= ok.mean()) + 1) / (N_SHUFFLE + 1)) if msk.any() else 1.0}
        edge = (acc - base) * 100
        passed = bool(edge >= PASS_EDGE_PTS and pval < PASS_P and
                      (not PASS_HALVES or all(hh["acc"] > hh["base"] for hh in halves)))
        res["outcomes"][name] = {
            "accuracy": acc, "ci95": [acc - 1.96 * se, acc + 1.96 * se], "n": n,
            "base_rate": base, "edge_pts": edge, "shuffle_mean": base, "p_value": pval,
            "always_long": float((mk["ret"] > 0).mean()), "halves": halves, "by_type": by_type, "passed": passed,
        }
    res["passed_any"] = any(v["passed"] for v in res["outcomes"].values())
    res["best"] = max(res["outcomes"], key=lambda k: res["outcomes"][k]["edge_pts"])
    return res


def report(res: dict) -> str:
    L = [f"NIFTY daily-bias backtest · {res['days']} days · {res['from']} → {res['to']}",
         f"Signals: BULL {res['signals']['BULL']} · BEAR {res['signals']['BEAR']} · no read {res['signals']['NONE']}", ""]
    for name, o in res["outcomes"].items():
        L.append(f"[{name}] accuracy {o['accuracy'] * 100:.1f}%  (95% CI {o['ci95'][0] * 100:.1f}–{o['ci95'][1] * 100:.1f}, n={o['n']})")
        L.append(f"   random-chance level {o['base_rate'] * 100:.1f}% (same candles, tomorrow shuffled) · edge {o['edge_pts']:+.1f} pts · p={o['p_value']:.3f}"
                 + (f" · always-long {o['always_long'] * 100:.1f}%" if name == "dir" else ""))
        L.append(f"   halves: " + " | ".join(f"{h['acc'] * 100:.1f}% vs {h['base'] * 100:.1f}%" for h in o["halves"]))
        for tn in ("BULL", "BEAR"):
            bt_ = o["by_type"][tn]
            L.append(f"   {tn} calls {bt_['acc'] * 100:.1f}% vs chance {bt_['chance'] * 100:.1f}% "
                     f"(edge {(bt_['acc'] - bt_['chance']) * 100:+.1f} pts, p={bt_['p']:.3f}, n={bt_['n']})")
        L.append(f"   → {'PASSED ✔' if o['passed'] else 'NOT an edge ✘'}")
    L.append("")
    dirp, drawp = res["outcomes"]["dir"]["passed"], res["outcomes"]["draw"]["passed"]
    if dirp:
        v = "yesterday's candle predicts the DIRECTION of today's close → used as a direction filter in Rule 1."
    elif drawp:
        bt_ = res["outcomes"]["draw"]["by_type"]
        used = [f"{'PDH after BULL' if k == 'BULL' else 'PDL after BEAR'}" for k in ("BULL", "BEAR")
                if (bt_[k]["acc"] - bt_[k]["chance"]) * 100 >= 3.0 and bt_[k]["p"] <= 0.01]
        weak = [k for k in ("BULL", "BEAR") if k not in [u.split()[-1] for u in used]]
        v = ("yesterday's candle does NOT predict direction, but it DOES predict which level gets taken (the 'draw'). "
             f"Used as a TARGET: {', '.join(used) or 'none'}"
             + (f"; too weak per side (edge < 3 pts or p > 0.01): {', '.join(weak)} reads." if weak else "."))
    else:
        v = "no reliable edge on this NIFTY data → shown as INFO only."
    L.append("VERDICT: " + v)
    return "\n".join(L)


if __name__ == "__main__":
    import runlog
    runlog.start("backtest_daily_bias")
    p = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    d = load(p)
    res = evaluate(d)
    print(report(res))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, indent=1, default=float))
    print(f"\nSaved {OUT}")
