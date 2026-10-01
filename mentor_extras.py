"""
mentor_extras.py — the rules from his LAST class (the longer "Lecture 6" recording: shifting recap, double
bottom entries, RSI divergences, monthly-expiry straddle), each with a history check.

  .venv/bin/python mentor_extras.py               # today's readings
  .venv/bin/python mentor_extras.py --backtest    # daily since 2019 + 1 year of 15-min

His rules (numbers in mentor_extras_rules.json):
  RSI divergence       tops: price higher high, RSI lower high -> down (7-8 of 10)
  positive divergence  lows: price higher low, RSI lower low -> up
  negative divergence  highs: price lower high, RSI higher high -> down
  expiry straddle      at the open of the Nifty monthly-expiry day buy a next-week call + put ~50 pts OTM;
                       futures move +-300 from that open before the next weekly expiry (Bank Nifty +-600);
                       best when India VIX is low (11-12), skip high VIX (15-16) and the October week
  red expiry BTST      (a student's idea he said he'd check) monthly expiry closes red -> buy a call at the
                       close for the next day
  double bottom        (final-class wording) exit when a candle CLOSES below the bottom candle
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

import mentor_structure as ms
import mentor_sweeps as msw

log = logging.getLogger("mentor_extras")
HERE = Path(__file__).resolve().parent
RULES_PATH = HERE / "mentor_extras_rules.json"


def rules() -> dict:
    return json.loads(RULES_PATH.read_text())


# ─────────────────────────────── RSI + divergences ──────────────────────────
def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def fractal_swings(c: pd.DataFrame, k: int) -> list[tuple[str, int, int]]:
    """Swing highs/lows with k candles each side; known k candles later (no look-ahead).
    Returns (kind, idx, confirm_idx)."""
    H, L = c["high"].to_numpy(float), c["low"].to_numpy(float)
    out = []
    for i in range(k, len(c) - k):
        if H[i] == H[i - k:i + k + 1].max():
            out.append(("H", i, i + k))
        if L[i] == L[i - k:i + k + 1].min():
            out.append(("L", i, i + k))
    return out


def divergences(c: pd.DataFrame, R: dict | None = None, tf: str = "15m") -> pd.DataFrame:
    """His three divergences between the last two swing highs (or lows), traded at the confirmation candle:
    entry = its close, SL beyond the second swing, target = the opposite swing between the two."""
    R = R or rules()
    D, K = R["divergence"], R["risk"]
    k, buf, min_rr = D["swing_bars_each_side"].get(tf, 3), K["sl_buffer_points"], K["min_rr"]
    hold = D["max_bars"].get(tf, 26)
    c = c.reset_index(drop=True)
    O, H, L, C = msw._arr(c)
    r = rsi(c["close"], D["rsi_length"]).to_numpy()
    sw = fractal_swings(c, k)
    highs = [s for s in sw if s[0] == "H"]
    lows = [s for s in sw if s[0] == "L"]
    rows = []

    def add(kind, side, a, b, conf, target):
        if conf >= len(c):
            return
        entry = C[conf]
        sl = (H[b] + buf) if side == "bear" else (L[b] - buf)
        reward = (entry - target) if side == "bear" else (target - entry)
        risk = abs(entry - sl)
        if reward <= 0 or risk <= 0 or (side == "bear" and entry >= sl) or (side == "bull" and entry <= sl):
            return
        rr = reward / risk
        res, _, rr_ = msw._replay(H, L, C, side, conf, entry, sl, target, hold)
        rows.append({"tf": tf, "kind": kind, "side": side, "swing1": a, "swing2": b, "idx": conf, "entry": entry,
                     "sl": sl, "target": target, "rr": round(rr, 2), "passes_rr": rr >= min_rr, "result": res, "r": rr_})

    for (_, a, _), (_, b, cb) in zip(highs, highs[1:]):
        if b - a > D["max_gap_bars"].get(tf, 60):
            continue
        tgt = L[a:b + 1].min()
        if H[b] > H[a] and r[b] < r[a]:
            add("RSI divergence (price HH, RSI LH)", "bear", a, b, cb, tgt)
        elif H[b] < H[a] and r[b] > r[a]:
            add("Negative divergence (price LH, RSI HH)", "bear", a, b, cb, tgt)
    for (_, a, _), (_, b, cb) in zip(lows, lows[1:]):
        if b - a > D["max_gap_bars"].get(tf, 60):
            continue
        tgt = H[a:b + 1].max()
        if L[b] > L[a] and r[b] < r[a]:
            add("Positive divergence (price HL, RSI LL)", "bull", a, b, cb, tgt)
    return pd.DataFrame(rows)


# ─────────────────────────────── monthly-expiry straddle ─────────────────────
def expiry_days(daily: pd.DataFrame) -> pd.DataFrame:
    import participant_correlation as pc
    cal = pc.expiry_calendar(pd.DatetimeIndex(pd.to_datetime(daily["timestamp"])), pc.rules())
    return cal


def expiry_straddle(daily: pd.DataFrame, R: dict | None = None, vix: pd.Series | None = None) -> pd.DataFrame:
    """From the open of each monthly-expiry day: did Nifty move +-N points (or the same % of price) before the
    next weekly expiry? Index daily bars (he measures on futures; the open gap differs a little)."""
    R = R or rules()
    E = R["expiry_straddle"]
    d = daily.reset_index(drop=True).copy()
    d["timestamp"] = pd.to_datetime(d["timestamp"]).dt.normalize()
    cal = expiry_days(d)
    d = d.set_index("timestamp").join(cal)
    ts = list(d.index)
    rows = []
    for i, t in enumerate(ts):
        if not d.at[t, "is_monthly_expiry"]:
            continue
        j = next((m for m in range(i + 1, len(ts)) if d.at[ts[m], "is_expiry"]), None)
        if j is None:
            continue
        w = d.iloc[i:j + 1]
        o = d.at[t, "open"]
        pts = E["points"]
        pct_pts = o * E["points"] / E["points_reference_level"]
        up, dn = w["high"].max() - o, o - w["low"].min()
        row = {"expiry": t.date(), "open": o, "window_days": len(w), "max_up": round(up), "max_down": round(dn),
               f"hit_{pts}": bool(up >= pts or dn >= pts), "hit_scaled": bool(up >= pct_pts or dn >= pct_pts),
               "scaled_points": round(pct_pts), "october": t.month == 10}
        if vix is not None and t in vix.index:
            row["vix"] = float(vix.loc[t])
        rows.append(row)
    return pd.DataFrame(rows)


def red_expiry_btst(daily: pd.DataFrame, R: dict | None = None) -> pd.DataFrame:
    """Student's rule: monthly expiry day closes red -> buy a call at the close; how far does the next day go up?"""
    R = R or rules()
    d = daily.reset_index(drop=True).copy()
    d["timestamp"] = pd.to_datetime(d["timestamp"]).dt.normalize()
    cal = expiry_days(d)
    d = d.set_index("timestamp").join(cal)
    d["next_up"] = d["high"].shift(-1) - d["close"]
    d["next_close"] = d["close"].shift(-1) - d["close"]
    d["red"] = d["close"] < d["open"]
    x = d[d["next_up"].notna()]
    return x[["open", "close", "red", "is_monthly_expiry", "next_up", "next_close"]].reset_index()


# ─────────────────────────────── double bottom, close-based SL ──────────────
def double_close_sl(c: pd.DataFrame, R: dict | None = None, tf: str = "15m") -> pd.DataFrame:
    """Final-class wording of his double bottom: enter in the bottom candle, exit only when a candle CLOSES below it
    (above it for a top). Uses the same zones as mentor_sweeps.double_zones (entry on the retest, target the
    latest swing high); R = distance to the zone edge + buffer."""
    SR = msw.rules()
    z = msw.double_zones(c, SR, tf, intraday=(tf == "15m"))
    if not len(z):
        return z
    O, H, L, C = msw._arr(c)
    hold = SR["double_bottom"]["max_bars_in_trade"]
    rows = []
    for _, r in z[z["status"].isin(["TAKEN", "SKIPPED (R:R < 1)"])].iterrows():
        bull, k = r["side"] == "bull", int(r["entry_idx"])
        entry, target, risk = r["entry"], r["target"], abs(r["entry"] - r["sl"])
        res, out = "TIMEOUT", None
        for m in range(k, min(len(C), k + hold + 1)):
            if (bull and H[m] >= target and m > k) or (not bull and L[m] <= target and m > k):
                res, out = "TARGET", abs(target - entry) / risk
                break
            if (bull and C[m] < r["zone_lo"]) or (not bull and C[m] > r["zone_hi"]):
                res, out = "CLOSED THROUGH", ((C[m] - entry) if bull else (entry - C[m])) / risk
                break
        if out is None:
            e = min(len(C) - 1, k + hold)
            out = ((C[e] - entry) if bull else (entry - C[e])) / risk
        rows.append({"side": r["side"], "status": r["status"], "result": res, "r": out, "touch_r": r["r"]})
    return pd.DataFrame(rows)


# ─────────────────────────────── history check ──────────────────────────────
def _vix() -> pd.Series | None:
    try:
        import participant_correlation as pc
        v = pc._yf_daily("^INDIAVIX")
        return v["close"] if v is not None else None
    except Exception as exc:
        log.info("India VIX unavailable: %s", exc)
        return None


def backtest(R: dict | None = None) -> dict:
    R = R or rules()
    minimum = R["history"]["min_sample_size"]
    out = {"divergence": [], "expiry": [], "double_close": []}
    frames = {}
    h15 = ms.load_history(ms.rules())
    if h15 is not None and len(h15) > 500:
        frames["15m"] = h15.reset_index(drop=True)
    daily = None
    try:
        import participant_correlation as pc
        dd = pc.load_ohlc()
        daily = msw.daily_frame(dd) if dd is not None else None
    except Exception as exc:
        log.warning("daily unavailable: %s", exc)
    if daily is not None:
        frames["1d"] = daily
    for tf, c in frames.items():
        for lab, cc in (("", c), ("CONTROL: ", msw._shuffle(c))):
            dv = divergences(cc, R, tf)
            if not len(dv):
                continue
            for kind in dv["kind"].unique():
                x = dv[(dv["kind"] == kind) & dv["passes_rr"]]
                row = msw._stats(x, minimum, lab + kind, tf)
                row["Moved his way first %"] = round(100 * (x["result"] == "TARGET").mean(), 1) if len(x) >= minimum \
                    else "INSUFFICIENT SAMPLE"
                out["divergence"].append(row)
        dc = double_close_sl(c, R, tf) if tf in ("15m", "1d") else pd.DataFrame()
        if len(dc):
            t = dc[dc["status"] == "TAKEN"]
            out["double_close"].append({"Double bottom/top exit rule": "Exit on a CLOSE through the candle (final class)",
                                        "TF": tf, "Trades": len(t), "Avg R per trade": round(float(t["r"].mean()), 2)
                                        if len(t) >= minimum else "INSUFFICIENT SAMPLE",
                                        "Target hit %": round(100 * (t["result"] == "TARGET").mean(), 1)
                                        if len(t) >= minimum else ""})
            out["double_close"].append({"Double bottom/top exit rule": "Exit on a TOUCH beyond the candle (Lecture 7)",
                                        "TF": tf, "Trades": len(t), "Avg R per trade": round(float(t["touch_r"].mean()), 2)
                                        if len(t) >= minimum else "INSUFFICIENT SAMPLE", "Target hit %": ""})
    if daily is not None:
        vix = _vix()
        ex = expiry_straddle(daily, R, vix)
        if len(ex):
            pts = R["expiry_straddle"]["points"]
            recent = ex[pd.to_datetime(ex["expiry"]) >= pd.Timestamp(R["expiry_straddle"]["recent_from"])]
            for lab, x in (("All monthly expiries since 2019", ex), (f"Since {R['expiry_straddle']['recent_from']}", recent),
                           ("  excluding October (he skips it)", ex[~ex["october"]]), ("  October only", ex[ex["october"]])):
                out["expiry"].append({"Monthly-expiry straddle": lab, "Cases": len(x),
                                      f"Hit +-{pts} pts %": round(100 * x[f"hit_{pts}"].mean(), 1) if len(x) else "",
                                      "Hit same % of price %": round(100 * x["hit_scaled"].mean(), 1) if len(x) else "",
                                      "Median best move (pts)": int(np.median(np.maximum(x["max_up"], x["max_down"])))
                                      if len(x) else ""})
            if "vix" in ex:
                lo_ = ex[ex["vix"] <= R["expiry_straddle"]["low_vix"]]
                hi_ = ex[ex["vix"] >= R["expiry_straddle"]["high_vix"]]
                for lab, x in ((f"  India VIX <= {R['expiry_straddle']['low_vix']}", lo_),
                               (f"  India VIX >= {R['expiry_straddle']['high_vix']}", hi_)):
                    out["expiry"].append({"Monthly-expiry straddle": lab, "Cases": len(x),
                                          f"Hit +-{pts} pts %": round(100 * x[f"hit_{pts}"].mean(), 1) if len(x) else "",
                                          "Hit same % of price %": round(100 * x["hit_scaled"].mean(), 1) if len(x) else "",
                                          "Median best move (pts)": int(np.median(np.maximum(x["max_up"], x["max_down"])))
                                          if len(x) else ""})
        bt = red_expiry_btst(daily, R)
        if len(bt):
            thr = R["red_expiry_btst"]["next_day_up_points"]
            for lab, x in (("Monthly expiry closed RED -> next day", bt[bt["is_monthly_expiry"] & bt["red"]]),
                           ("Monthly expiry closed green -> next day", bt[bt["is_monthly_expiry"] & ~bt["red"]]),
                           ("Any red day -> next day (baseline)", bt[bt["red"]])):
                out["expiry"].append({"Monthly-expiry straddle": lab, "Cases": len(x),
                                      f"Hit +-{pts} pts %": f"next-day high >= close+{thr}: "
                                      f"{100 * (x['next_up'] >= thr).mean():.0f}%" if len(x) else "",
                                      "Hit same % of price %": f"next close up: {100 * (x['next_close'] > 0).mean():.0f}%"
                                      if len(x) else "",
                                      "Median best move (pts)": int(x["next_up"].median()) if len(x) else ""})
    res = {k: pd.DataFrame(v) for k, v in out.items()}
    res["span"] = {tf: (c["timestamp"].min(), c["timestamp"].max(), len(c)) for tf, c in frames.items()}
    return res


def live(c15: pd.DataFrame | None, daily: pd.DataFrame | None, R: dict | None = None) -> dict:
    R = R or rules()
    out = {}
    for tf, c in (("15m", c15), ("1d", daily)):
        if c is None or len(c) < 40:
            continue
        dv = divergences(c.reset_index(drop=True), R, tf)
        out[f"div_{tf}"] = dv[dv["idx"] >= len(c) - (26 if tf == "15m" else 5)] if len(dv) else dv
        out[f"rsi_{tf}"] = float(rsi(c["close"], R["divergence"]["rsi_length"]).iloc[-1])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    pd.set_option("display.width", 220, "display.max_columns", 20, "display.max_colwidth", 60)
    if a.backtest:
        b = backtest()
        for tf, (f, t, n) in b["span"].items():
            print(f"{tf}: {n:,} bars {f:%d-%b-%Y} .. {t:%d-%b-%Y}")
        for k in ("divergence", "double_close", "expiry"):
            print(f"\n== {k} ==")
            print(b[k].to_string(index=False))
        return 0
    c, _ = ms.recent_candles(10)
    try:
        import participant_correlation as pc
        dd = pc.load_ohlc()
        daily = msw.daily_frame(dd) if dd is not None else None
    except Exception:
        daily = None
    L = live(c, daily)
    for k, v in L.items():
        print(k, v if not isinstance(v, pd.DataFrame) else v.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
