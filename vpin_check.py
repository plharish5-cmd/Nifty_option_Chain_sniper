"""
vpin_check.py — test the app's VPIN (vpin.calculate_vpin) two ways, without touching vpin.py or the tab:

  1. the seven Stage-1 tests from "VPIN Fix v2" (synthetic data, PASS/FAIL with the actual numbers)
  2. does it WORK on your stored futures volume: after CDF[VPIN] >= threshold, is the next 30 / 60 minutes
     bigger than usual, and does DIR's sign call the direction? Compared with (a) all other buckets at the
     same time of day and (b) VPIN recomputed on the same days with the volume shuffled inside each day
     (destroys the order-flow information, keeps everything else).

  .venv/bin/python vpin_check.py            # both
  .venv/bin/python vpin_check.py --tests    # only the Stage-1 tests
"""
from __future__ import annotations

import argparse
import copy
import logging
from datetime import timedelta

import numpy as np
import pandas as pd

import vpin

log = logging.getLogger("vpin_check")


# ─────────────────────────────── synthetic data ─────────────────────────────
def _session(day: str, n: int = 375, start: float = 24000.0, seed: int = 1, flat: bool = False,
             vol: float = 2000.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    ts = pd.date_range(f"{day} 09:15", periods=n, freq="1min")
    px = np.full(n, start) if flat else start + np.cumsum(rng.normal(0, 4, n))
    v = rng.integers(int(vol * 0.5), int(vol * 1.5), n).astype(float)
    return pd.DataFrame({"timestamp": ts, "close": px, "volume": v})


def _cfg(**kw) -> dict:
    c = copy.deepcopy(vpin.load_vpin_config())
    c.update(kw)
    return c


def _b(df, cfg=None):
    return vpin.calculate_vpin(df, cfg or _cfg())


# ─────────────────────────────── Stage-1 tests ──────────────────────────────
def run_tests() -> list[tuple[str, bool, str]]:
    out = []

    def rec(name, ok, detail):
        out.append((name, bool(ok), detail))

    cfg = _cfg(bucket_volume=20000.0, sigma_window=50, n_buckets=10, cdf_window=30)
    s1, s2 = _session("2026-09-01", seed=1), _session("2026-09-02", start=24150.0, seed=2)
    two = pd.concat([s1, s2], ignore_index=True)
    base = _b(two, cfg).buckets

    # 1. cumulative feed: volume given as a running total that resets every morning
    cum = two.copy()
    cum["volume"] = cum.groupby(cum["timestamp"].dt.date)["volume"].cumsum()
    try:
        cb = _b(cum, cfg).buckets
        rec("1 cumulative feed (resets daily) is differenced", len(cb) == len(base),
            f"per-bar volume -> {len(base)} buckets; same data as a daily running total -> {len(cb)} buckets")
    except Exception as exc:
        rec("1 cumulative feed (resets daily) is differenced", False, f"raised {exc!r}")
    one_cum = s1.copy()
    one_cum["volume"] = one_cum["volume"].cumsum()
    b1 = _b(s1, cfg).buckets
    b1c = _b(one_cum, cfg).buckets
    rec("1b cumulative feed (one session)", len(b1c) == len(b1), f"{len(b1)} vs {len(b1c)} buckets")

    # 2. frozen feed: the last row repeated 500 times (same timestamp), and 500 more polls with the clock moving
    #    but the cumulative volume frozen
    frozen = pd.concat([one_cum] + [one_cum.iloc[[-1]]] * 500, ignore_index=True)
    fb = _b(frozen, cfg).buckets
    t = one_cum.iloc[-1]
    moving = pd.DataFrame({"timestamp": [t["timestamp"] + timedelta(seconds=5 * (k + 1)) for k in range(500)],
                           "close": t["close"], "volume": t["volume"]})
    mb = _b(pd.concat([one_cum, moving], ignore_index=True), cfg).buckets
    rec("2 frozen feed adds zero buckets", len(fb) == len(b1c) and len(mb) == len(b1c),
        f"clean {len(b1c)}, +500 identical rows {len(fb)}, +500 polls with frozen volume {len(mb)}")

    # 3. flat price -> 50/50 split, VPIN ~ 0 (or no value when sigma is 0)
    flat = _b(_session("2026-09-03", flat=True), cfg).buckets
    if len(flat):
        bf = (flat["v_buy"] / flat["total_vol"]).dropna()
        vp = flat["vpin"].dropna()
        ok = (bf.empty or bf.between(0.45, 0.55).all()) and (vp.empty or vp.abs().max() < 0.05)
        rec("3 flat price -> buy share 0.45-0.55, VPIN ~ 0", ok,
            f"{len(flat)} buckets; buy share {bf.min() if len(bf) else 'n/a'}..{bf.max() if len(bf) else 'n/a'}; "
            f"max VPIN {vp.abs().max() if len(vp) else 'n/a (None, sigma = 0)'}")
    else:
        rec("3 flat price -> buy share 0.45-0.55, VPIN ~ 0", True, "no buckets formed (acceptable: sigma = 0)")

    # 4. sign invariant |DIR| <= VPIN, strictly less on mixed-sign windows
    x = base.dropna(subset=["vpin", "dir"])
    viol = (x["dir"].abs() > x["vpin"] + 1e-9).sum()
    equal = np.isclose(x["dir"].abs(), x["vpin"]).mean() if len(x) else np.nan
    rec("4 |DIR| <= VPIN (never DIR == VPIN on mixed data)", len(x) > 0 and viol == 0 and equal < 0.5,
        f"{len(x)} buckets with values; violations {viol}; share with |DIR| == VPIN {equal:.0%}")

    # 5. session boundary: 150-point overnight gap
    st = pd.to_datetime(base["start_time"]).dt.date
    en = pd.to_datetime(base["end_time"]).dt.date
    span = int((st != en).sum())
    gap_first = base.groupby("session_date").head(1)
    sig_one = _b(pd.concat([s1, s2.assign(close=s2["close"] - 150)], ignore_index=True), cfg).buckets
    same_vpin = np.allclose(base["vpin"].fillna(-1).values[:len(sig_one)], sig_one["vpin"].fillna(-1).values[:len(base)])
    rec("5 no bucket spans two sessions; gap does not change the result", span == 0 and same_vpin,
        f"buckets spanning the night {span}; identical VPIN with the gap removed: {same_vpin}")

    # 6. bucket integrity
    V = cfg["bucket_volume"]
    tot_b = float(base["total_vol"].sum())
    tot_v = float(two["volume"].sum())
    dup_end = int(base.duplicated(["end_time"]).sum())
    full = np.isclose(base["total_vol"], V).all()
    # with bucket_volume null the first session only sizes the buckets, so compare against the sessions bucketed
    tot_v = float(two[two["timestamp"].dt.date.isin(set(pd.to_datetime(base["end_time"]).dt.date))]["volume"].sum())
    big = two.copy()
    big.loc[100, "volume"] = 3.5 * V
    bb = _b(big, cfg).buckets
    rec("6 buckets == V, total <= bar volume, no duplicate end times, a huge bar fills several",
        full and tot_b <= tot_v + 1e-6 and tot_v - tot_b < 2 * V and dup_end == 0 and len(bb) >= len(base) + 2,
        f"all == V: {full}; bucket vol {tot_b:,.0f} vs bar vol of bucketed sessions {tot_v:,.0f} (unfinished <= 1 bucket/session); "
        f"duplicate end times {dup_end}; one 3.5V bar: {len(base)} -> {len(bb)} buckets")

    # 7. no look-ahead: bucket k scored on data cut at its end == scored on all data
    #    (with the app's own setting, bucket_volume from strategy1_rules.json)
    app_cfg = _cfg(sigma_window=50, n_buckets=10, cdf_window=30)
    full_b = _b(two, app_cfg).buckets.dropna(subset=["vpin"])
    bad, checked = 0, 0
    for k in list(full_b.index[:: max(1, len(full_b) // 8)])[:8]:
        row = full_b.loc[k]
        cut = two[two["timestamp"] <= pd.Timestamp(row["end_time"])]
        cb2 = _b(cut, app_cfg).buckets
        m = cb2[pd.to_datetime(cb2["end_time"]) == pd.Timestamp(row["end_time"])]
        checked += 1
        if not len(m) or not np.isclose(m["vpin"].iloc[0], row["vpin"]):
            bad += 1
    auto = app_cfg.get("bucket_volume") in (None, 0)
    rec("7 no look-ahead (app setting: bucket_volume " + ("auto = ADV/buckets_per_day" if auto else "fixed") + ")",
        bad == 0, f"{checked} buckets re-scored on data cut at their close: {bad} differ"
        + (" - V is derived from the average daily volume of ALL rows, including later days" if bad and auto else ""))
    # 8. does VPIN / CDF actually come out on normal multi-day data?
    many = pd.concat([_session(str(d.date()), seed=i, start=24000 + 5 * i)
                      for i, d in enumerate(pd.bdate_range("2026-06-01", periods=20))], ignore_index=True)
    c8 = _cfg(bucket_volume=15000.0)
    b8 = _b(many, c8).buckets
    after = b8.iloc[c8["n_buckets"] + c8["cdf_window"]:] if len(b8) else b8
    av_v = after["vpin"].notna().mean() if len(after) else 0
    av_c = after["cdf"].notna().mean() if len(after) else 0
    rec("8 VPIN and CDF are available after warm-up (20 clean days)", av_v > 0.8 and av_c > 0.8,
        f"{len(b8)} buckets; after the {c8['n_buckets']}+{c8['cdf_window']}-bucket warm-up: VPIN on {av_v:.0%}, "
        f"CDF on {av_c:.0%}" + (" - each day's first bucket is marked invalid (it holds the 09:15 bar) and any "
                                 "window touching one returns None, so a 50-bucket window almost never qualifies"
                                 if av_v < 0.8 else ""))
    return out


# ─────────────────────────────── reference VPIN (spec formulas) ─────────────
def ref_vpin(f: pd.DataFrame, V: float, cfg: dict) -> pd.DataFrame:
    """Clean VPIN straight from the spec, used only for the study: buckets per session, the 09:15 bar left out,
    sigma from intraday changes only, VPIN over the last n buckets, CDF over the previous cdf_window VPINs."""
    f = f.sort_values("timestamp").reset_index(drop=True)
    day = f["timestamp"].dt.date
    ds = f.groupby(day)["close"].diff()
    sig = ds.dropna().rolling(int(cfg["sigma_window"]), min_periods=int(cfg["sigma_window"])).std().reindex(f.index)
    sig = sig.groupby(day).ffill()
    from scipy.stats import norm
    z = ds / sig
    buy = pd.Series(norm.cdf(z.fillna(0)), index=f.index).where(sig > 1e-9)
    rows = []
    T, P, VOL, B, D = f["timestamp"].values, f["close"].values, f["volume"].values, buy.values, day.values
    cur_v = cur_b = 0.0
    for i in range(len(f)):
        if i and D[i] != D[i - 1]:
            cur_v = cur_b = 0.0                      # partial bucket at the close is discarded
        if np.isnan(B[i]):
            continue                                 # first bar of the day / sigma warm-up: not classified
        rem = VOL[i]
        while rem > 0:
            take = min(rem, V - cur_v)
            cur_v += take
            cur_b += take * B[i]
            rem -= take
            if cur_v >= V - 1e-9:
                rows.append({"end_time": pd.Timestamp(T[i]), "end_price": P[i], "I": (2 * cur_b - V) / V})
                cur_v = cur_b = 0.0
    b = pd.DataFrame(rows)
    if not len(b):
        return b
    n, w = int(cfg["n_buckets"]), int(cfg["cdf_window"])
    b["vpin"] = b["I"].abs().rolling(n).mean()
    b["dir"] = b["I"].rolling(n).mean()
    v = b["vpin"].values
    b["cdf"] = [np.nan if k < n + w or np.isnan(v[k]) else float(np.mean(v[k - w:k] <= v[k])) for k in range(len(b))]
    return b


# ─────────────────────────────── does it work ───────────────────────────────
def futures_minutes() -> pd.DataFrame | None:
    try:
        import mentor_sweeps as msw
        f = msw.futures_minutes(0)
    except Exception as exc:
        log.warning("futures minutes unavailable: %s", exc)
        return None
    if f is None:
        return None
    return f[["timestamp", "close", "volume"]].sort_values("timestamp").reset_index(drop=True)


def _events(b: pd.DataFrame, f: pd.DataFrame, thr: float, horizons=(30, 60)) -> pd.DataFrame:
    """For every completed bucket: move and range over the next h minutes (same session only)."""
    f = f.set_index("timestamp")
    rows = []
    for _, x in b.dropna(subset=["cdf"]).iterrows():
        t0 = pd.Timestamp(x["end_time"])
        day = f[f.index.date == t0.date()]
        p0 = day.loc[:t0, "close"]
        if not len(p0):
            continue
        r = {"end_time": t0, "cdf": x["cdf"], "dir": x["dir"], "vpin": x["vpin"], "hot": x["cdf"] >= thr,
             "minute": t0.hour * 60 + t0.minute}
        for h in horizons:
            w = day.loc[t0 + timedelta(minutes=1): t0 + timedelta(minutes=h), "close"]
            if len(w) < h * 0.6:
                r[f"move{h}"] = r[f"range{h}"] = np.nan
                continue
            r[f"move{h}"] = float(w.iloc[-1] - p0.iloc[-1])
            r[f"range{h}"] = float(w.max() - w.min())
        rows.append(r)
    return pd.DataFrame(rows)


def _summ(e: pd.DataFrame, lab: str, h: int) -> dict:
    hot, cold = e[e["hot"]], e[~e["hot"]]
    # time-of-day matched baseline: each hot bucket vs cold buckets within 30 minutes of the same clock time
    matched = []
    for _, x in hot.iterrows():
        c = cold[(cold["minute"] - x["minute"]).abs() <= 30][f"range{h}"].dropna()
        if len(c):
            matched.append(x[f"range{h}"] / c.mean())
    d = hot.dropna(subset=[f"move{h}"])
    d = d[d["dir"].abs() > 0]
    hit = (np.sign(d["dir"]) == np.sign(d[f"move{h}"])).mean() if len(d) else np.nan
    return {"Run": lab, "Horizon": f"{h} min", "Hot buckets": len(hot), "Other buckets": len(cold),
            f"Range after hot (pts)": round(hot[f"range{h}"].mean(), 1) if len(hot) else "",
            f"Range after others (pts)": round(cold[f"range{h}"].mean(), 1) if len(cold) else "",
            "Hot / same-time others": round(float(np.nanmean(matched)), 2) if matched else "",
            "DIR sign called direction %": round(100 * hit, 1) if len(d) else ""}


def study() -> dict:
    f = futures_minutes()
    if f is None or len(f) < 5000:
        return {"error": "no stored futures minutes with volume (data/candles)"}
    days = f["timestamp"].dt.date
    per_day = f.groupby(days)["volume"]
    cum_like = (per_day.apply(lambda v: v.is_monotonic_increasing).mean())
    cfg = copy.deepcopy(vpin.load_vpin_config())
    thr = float(cfg["cdf_threshold"])
    out = {"span": (f["timestamp"].min(), f["timestamp"].max(), len(f), days.nunique()),
           "volume_looks_cumulative_share_of_days": round(float(cum_like), 2), "threshold": thr}
    # work around the two failed tests so the study itself is clean:
    #  (1) difference a daily running total per day (the app only checks the whole frame)
    #  (7) fix V from the first `warm` days and score only the days after them (no look-ahead)
    f = f.drop_duplicates("timestamp").reset_index(drop=True)
    if cum_like > 0.5:
        d = f.groupby(f["timestamp"].dt.date)["volume"].diff()
        f["volume"] = d.fillna(f["volume"]).clip(lower=0)
    f = f[f["volume"] > 0].reset_index(drop=True)
    days = f["timestamp"].dt.date
    warm = 10
    first = sorted(days.unique())[:warm]
    cfg["bucket_volume"] = float(f[days.isin(first)].groupby(days)["volume"].sum().median()) / float(cfg["buckets_per_day"])
    out["scored_from"] = sorted(days.unique())[warm] if days.nunique() > warm else None
    try:
        app = vpin.calculate_vpin(f, cfg).buckets
        out["app"] = (len(app), float(app["vpin"].notna().mean()) if len(app) else 0.0,
                      float(app["cdf"].notna().mean()) if len(app) else 0.0)
    except Exception as exc:
        out["app"] = (0, 0.0, 0.0)
        out["app_error"] = repr(exc)[:200]
    V = cfg["bucket_volume"]
    out["bucket_volume"] = V
    keep = lambda b: b[pd.to_datetime(b["end_time"]).dt.date >= out["scored_from"]] if out["scored_from"] else b
    real = ref_vpin(f, V, cfg)
    out["buckets"] = len(real)
    e = _events(keep(real), f, thr) if len(real) else pd.DataFrame()
    rng = np.random.default_rng(11)
    sh = f.copy()
    sh["volume"] = sh.groupby(days)["volume"].transform(lambda v: v.sample(frac=1, random_state=int(rng.integers(1e9))).values)
    ctrl = ref_vpin(sh, V, cfg)
    ec = _events(keep(ctrl), f, thr) if len(ctrl) else pd.DataFrame()
    rows = []
    for h in (30, 60):
        if len(e):
            rows.append(_summ(e, "VPIN (reference, your data)", h))
        if len(ec):
            rows.append(_summ(ec, "CONTROL: volume shuffled inside each day", h))
    out["table"] = pd.DataFrame(rows)
    out["events"] = e
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tests", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
    pd.set_option("display.width", 230, "display.max_columns", 20, "display.max_colwidth", 70)
    print("== Stage-1 tests on vpin.calculate_vpin ==")
    res = run_tests()
    for name, ok, det in res:
        print(f"{'PASS' if ok else 'FAIL'}  {name}\n      {det}")
    print(f"{sum(ok for _, ok, _ in res)}/{len(res)} passed")
    if a.tests:
        return 0
    print("\n== Does it work on your stored futures volume? ==")
    s = study()
    if "error" in s:
        print(s["error"])
        return 1
    f0, f1, n, nd = s["span"]
    print(f"futures 1-min: {n:,} bars, {nd} days, {f0:%d-%b-%Y} .. {f1:%d-%b-%Y}; "
          f"volume column looks cumulative on {s['volume_looks_cumulative_share_of_days']:.0%} of days")
    n_app, av_v, av_c = s["app"]
    print(f"YOUR APP'S vpin.py on this data: {n_app:,} buckets, VPIN on {av_v:.0%}, CDF on {av_c:.0%}"
          + (f"  ERROR {s['app_error']}" if s.get("app_error") else ""))
    print("Study below uses a clean reference VPIN (same formulas, the two bugs above fixed).")
    print(f"buckets: {s['buckets']:,} (V = {s['bucket_volume']:,.0f}, fixed from the first 10 days; scored from "
          f"{s['scored_from']}); 'hot' = CDF >= {s['threshold']}")
    print(s["table"].to_string(index=False))
    print("\nHow to read it: 'Hot / same-time others' > 1.2 on the VPIN rows and ~1.0 on the CONTROL = VPIN spots\n"
          "bigger-than-usual moves. 'DIR sign called direction %' has to be clearly above 50 to mean anything.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
