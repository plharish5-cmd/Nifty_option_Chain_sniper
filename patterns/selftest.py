"""Offline self-test on synthetic data. Run: python -m patterns.selftest"""
from __future__ import annotations

import shutil
import tempfile

import numpy as np
import pandas as pd

from patterns import smc_adapter, talib_patterns
from patterns.backtest_early import report, run as bt_run
from patterns.common import closed_1m, load_rules, normalize, resample
from patterns.early_read import build_path_library, early_read, log_read, reconcile

RNG = np.random.default_rng(1)
OK = []


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    OK.append(msg)
    print("PASS", msg)


def day_walk(date, start_px, sigma=4.0):
    idx = pd.date_range(f"{date} 09:15", f"{date} 15:29", freq="1min")
    c = start_px + np.cumsum(RNG.normal(0, sigma, len(idx)))
    o = np.r_[start_px, c[:-1]]
    h = np.maximum(o, c) + np.abs(RNG.normal(0, 2, len(idx)))
    l = np.minimum(o, c) - np.abs(RNG.normal(0, 2, len(idx)))
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": 1000.0}, index=idx)


def planted_day(date, px, m12_close_offset):
    """Random walk to 11:00, bearish 15-min 11:00-11:15, then bullish engulfing 11:15-11:30."""
    d = day_walk(date, px)
    pre = d[d.index < f"{date} 11:00"]
    P = pre["close"].iloc[-1]
    rows = []
    # bearish candle: P -> P-30
    path = np.linspace(P, P - 30, 16)
    for i in range(15):
        o, c = path[i], path[i + 1]
        rows.append((o, max(o, c) + 1, min(o, c) - 1, c))
    # engulfing candle: open P-32, dip to P-35, then rise to P+offset at minute 12, P+15 at close
    target12 = P + m12_close_offset
    # closes for minutes 1..15; minute 12 close == target12 exactly
    closes = np.r_[np.linspace(P - 33, P - 35, 3), np.linspace(P - 35, target12, 10)[1:],
                   np.linspace(target12, P + 15, 4)[1:]]
    assert len(closes) == 15 and np.isclose(closes[11], target12)
    prev = P - 32
    for c in closes:
        o = prev
        rows.append((o, max(o, c) + 0.5, min(o, c) - 0.5, c))
        prev = c
    idx = pd.date_range(f"{date} 11:00", periods=30, freq="1min")
    mid = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    mid["volume"] = 1000.0
    post = day_walk(date, mid["close"].iloc[-1])
    post = post[post.index >= f"{date} 11:30"]
    return pd.concat([pre, mid, post]), P


def main():
    rules = load_rules()
    tmp = tempfile.mkdtemp()
    rules["log_dir"] = tmp
    days = pd.bdate_range("2026-08-03", periods=12)
    hist = pd.concat([day_walk(d.date(), 23400 + i * 10) for i, d in enumerate(days[:-1])])
    today = str(days[-1].date())

    # 1. resample alignment + counts
    b15 = resample(hist, 15)
    check(all(t.minute in (0, 15, 30, 45) for t in b15.index), "15-min buckets align to :00/:15/:30/:45")
    check(b15.index[0].strftime("%H:%M") == "09:15", "first 15-min bucket starts 09:15")
    check(int((b15.index.date == b15.index.date[0]).sum()) == 25, "25 fifteen-minute candles per session")

    # 2/3. forming + closed_1m
    far, P = planted_day(today, 23500, m12_close_offset=+12)
    now = pd.Timestamp(f"{today} 11:27:30")      # minute 12 of the 11:15 candle, 30 s in
    d1 = closed_1m(far, now)
    check(d1.index[-1] == pd.Timestamp(f"{today} 11:26"), "in-progress 1-min bar (11:27) dropped")
    b = resample(d1, 15, now)
    check(bool(b["forming"].iloc[-1]) and int(b["n1m"].iloc[-1]) == 12, "forming 15-min candle has 12 closed minutes")
    check(not b["forming"].iloc[:-1].any(), "all earlier candles closed")

    # 4. TA-Lib finds the planted engulfing on the COMPLETED candle
    done = resample(pd.concat([hist, far]), 15)
    vals = talib_patterns.detect(done, ["CDLENGULFING"])
    check(vals.loc[pd.Timestamp(f"{today} 11:15"), "CDLENGULFING"] > 0, "TA-Lib detects planted bullish engulfing")
    hits = talib_patterns.hits(done.assign(forming=False), rules, levels=[{"px": P - 36, "name": "OI wall"}])
    row = hits[(hits["time"] == pd.Timestamp(f"{today} 11:15")) & (hits["code"] == "CDLENGULFING")]
    check(len(row) == 1 and bool(row["at_level"].iloc[0]), "engulfing flagged at the nearby level")

    # 5/6. early read: far-from-flip vs near-flip
    full_far = pd.concat([hist, far])
    er_far = early_read(full_far, rules, now, hist_1m=hist, levels=[P, P + 20])
    check(er_far["status"] == "ok", "early read runs at minute 12")
    pe = er_far["patterns"]
    eng = pe[pe["code"] == "CDLENGULFING"]
    check(len(eng) == 1 and eng["state"].iloc[0] == "PRESENT", "engulfing PRESENT on provisional candle")
    check(er_far["paths_source"].startswith("your 1-min history"), "path library built from history")
    near, P2 = planted_day(today, 23500, m12_close_offset=+0.6)
    er_near = early_read(pd.concat([hist, near]), rules, now, hist_1m=hist)
    en = er_near["patterns"]
    en = en[en["code"] == "CDLENGULFING"]
    p_far = float(eng["prob"].iloc[0])
    p_near = float(en["prob"].iloc[0]) if len(en) else 0.0
    print(f"     lock prob far={p_far:.2f} near={p_near:.2f}")
    check(p_far > p_near + 0.15, "close far from flip point locks harder than close near it")
    lo = eng["holds_above"].iloc[0]
    check(lo is not None and abs(lo - P) <= 2.0, f"flip level ~ prior open ({lo:.1f} vs {P:.1f})")

    # 7. vectorised library == loop reference on one day
    one = hist[hist.index.date == hist.index.date[0]]
    lib = build_path_library(one, 3, 30)
    h, l, c = one["high"].to_numpy(), one["low"].to_numpy(), one["close"].to_numpy()
    ref = []
    for i in range(30, len(c) - 3 + 1):
        u = (h - l)[i - 30:i].mean()
        c0 = c[i - 1]
        ref.append(((c[i + 2] - c0) / u, (h[i:i + 3].max() - c0) / u, (l[i:i + 3].min() - c0) / u))
    check(np.allclose(lib, np.array(ref)), "vectorised path library matches loop reference")

    # 8. no look-ahead: future bars in the frame change nothing
    er_trunc = early_read(closed_1m(full_far, now), rules, now, hist_1m=hist)
    check(np.isclose(er_trunc["patterns"]["prob"].sum(), er_far["patterns"]["prob"].sum()),
          "early read identical with and without future bars present")

    # 9. SMC adapter
    s = smc_adapter.run(done, 5)
    check(set(s) >= {"swings", "structure", "fvg", "ob", "liquidity"}, "SMC adapter returns all five tables")
    if not s["structure"].empty:
        check((s["structure"]["break_time"] > s["structure"]["swing_time"]).all(), "BOS/CHoCH keyed on break bar after swing")
    if not s["fvg"].empty:
        check((s["fvg"]["confirmed_at"] > s["fvg"]["time"]).all(), "FVG confirms one bar after the gap candle")

    # 10. log + reconcile
    log_read(er_far, tmp)
    rec = reconcile(tmp, pd.Timestamp(today), full_far, rules, pd.Timestamp(f"{today} 15:30"))
    r_eng = rec[rec["code"] == "CDLENGULFING"]
    check(len(r_eng) == 1 and r_eng["held"].iloc[0] == "True", "reconcile marks the engulfing as held")

    # 11. historical backtest harness runs
    res = bt_run(pd.concat([hist, far]), rules, elapsed=12, n_sims=60, min_lib=500)
    calib, per = report(res)
    check(len(res) > 0 and not calib.empty, f"backtest harness produced {len(res)} pattern-reads")

    shutil.rmtree(tmp)
    print(f"\nAll {len(OK)} checks passed.")


if __name__ == "__main__":
    main()
