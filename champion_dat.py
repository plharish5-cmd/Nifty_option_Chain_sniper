"""
champion_dat.py — the "World Champion's method on NIFTY" video (Trading Siddhant), as a separate feature:
DIRECTION -> AREA -> TRIGGER, with a 5-year history check.

  .venv/bin/python champion_dat.py               # today's read: direction, discount/premium zone, any trigger
  .venv/bin/python champion_dat.py --backtest    # 5 years of 15-min candles vs 5 pooled shuffles

His framework (numbers in champion_dat_rules.json):
  1 DIRECTION  1-hour structure (the app's BOS/CHoCH engine on 1-hour candles): up / down / undecided.
               A change of direction needs a break of the extreme AND a retest (N shape); inside that = noise.
  2 AREA       latest 1-hour swing, Fibonacci retracement 0.705 / 0.79 / 0.886. In an uptrend the zone between
               0.705 and 0.886 is DISCOUNT = the buying area; a close beyond 0.886 = no trade (idea invalid).
               Mirror in a downtrend (PREMIUM = selling area). Never buy in premium / sell in discount.
  3 TRIGGER    a pin bar (rejection candle) on 15-min inside the zone, closing back in the trend's direction.
               (He confirms with footprint delta - aggressive sellers at the top of a bearish pin. We have no
               footprint data, so the candle alone is tested.)
  TARGET       the swing extreme (previous swing high for a buy), R:R 1.5-2; he wins 60-65% of trades.
  TIME         trade when volume is there: first ~1.5 h (09:15-11:00) and the post-lunch spike; dead volume = no trade.
  DISCIPLINE   no trade some days, no back-to-back trades, journal every trade (shown as a reminder, not coded).
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import date, time as dtime
from pathlib import Path

import numpy as np
import pandas as pd

import mentor_structure as ms
import mentor_sweeps as msw

log = logging.getLogger("champion_dat")
HERE = Path(__file__).resolve().parent
RULES_PATH = HERE / "champion_dat_rules.json"
VERDICT_PATH = HERE / "data" / "champion_dat_verdict.json"


def rules() -> dict:
    return json.loads(RULES_PATH.read_text())


def hourly(c15: pd.DataFrame) -> pd.DataFrame:
    """15-min -> 1-hour candles per session, anchored at 09:15 (09:15-10:15, ... 15:15-15:30)."""
    c = c15.copy()
    c["timestamp"] = pd.to_datetime(c["timestamp"])
    out = []
    for _, g in c.groupby(c["timestamp"].dt.date):
        r = g.set_index("timestamp").resample("60min", origin="start_day", offset="9h15min").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
        out.append(r)
    return pd.concat(out).reset_index()


def _fractals(H: np.ndarray, L: np.ndarray, k: int) -> tuple[list[int], list[int]]:
    hi = [j for j in range(k, len(H) - k) if H[j] == H[j - k:j + k + 1].max()]
    lo = [j for j in range(k, len(L) - k) if L[j] == L[j - k:j + k + 1].min()]
    return hi, lo


def context(c15: pd.DataFrame, R: dict | None = None) -> pd.DataFrame:
    """For every 15-min bar: the 1-hour direction and the fib zone known at that bar (no look-ahead)."""
    R = R or rules()
    F = R["area"]
    c15 = c15.reset_index(drop=True)
    h1 = hourly(c15)
    trend = msw.trend_series(h1)                       # 1-hour structure, known at each 1-hour close
    H, L = h1["high"].values, h1["low"].values
    k = F["swing_k"]
    his, los = _fractals(H, L, k)
    h_end = pd.to_datetime(h1["timestamp"]) + pd.Timedelta(minutes=60)
    rows = []
    for j in range(len(h1)):
        known = j                                       # a fractal at bar p is known at bar p + k
        up, dn = trend[j], trend[j]
        hs = [p for p in his if p + k <= known]
        ls = [p for p in los if p + k <= known]
        z = {"h_idx": j, "h_end": h_end.iloc[j], "dir": int(trend[j]), "sw_lo": np.nan, "sw_hi": np.nan}
        if hs and ls:
            if up > 0:                                  # swing low, then the highest high after it
                lo_p = ls[-1]
                hi_after = [p for p in hs if p > lo_p]
                hi_p = hi_after[-1] if hi_after else None
                if hi_p is not None:
                    hi_v = H[lo_p:known + 1].max()
                    z.update(sw_lo=L[lo_p], sw_hi=hi_v)
            elif dn < 0:
                hi_p = hs[-1]
                lo_after = [p for p in ls if p > hi_p]
                if lo_after:
                    lo_v = L[hi_p:known + 1].min()
                    z.update(sw_hi=H[hi_p], sw_lo=lo_v)
        rows.append(z)
    ctx = pd.DataFrame(rows)
    # map each 15-min bar to the last COMPLETED hour
    t15 = pd.to_datetime(c15["timestamp"]) + pd.Timedelta(minutes=15)
    pos = np.searchsorted(ctx["h_end"].values, t15.values, side="right") - 1
    m = ctx.iloc[np.clip(pos, 0, len(ctx) - 1)].reset_index(drop=True)
    m.loc[pos < 0, ["dir", "sw_lo", "sw_hi"]] = [0, np.nan, np.nan]
    same_day = pd.to_datetime(m["h_end"]).dt.date.values == pd.to_datetime(c15["timestamp"]).dt.date.values
    if R["area"].get("zone_from_previous_days", True) is False:
        m.loc[~same_day, ["sw_lo", "sw_hi"]] = np.nan
    lvl = F["levels"]
    rng_ = m["sw_hi"] - m["sw_lo"]
    up = m["dir"] > 0
    # retracement r measured from the swing extreme in the trend direction
    m["z_near"] = np.where(up, m["sw_hi"] - lvl[0] * rng_, m["sw_lo"] + lvl[0] * rng_)     # 0.705
    m["z_mid"] = np.where(up, m["sw_hi"] - lvl[1] * rng_, m["sw_lo"] + lvl[1] * rng_)      # 0.79
    m["z_far"] = np.where(up, m["sw_hi"] - lvl[2] * rng_, m["sw_lo"] + lvl[2] * rng_)      # 0.886 = invalidation
    m["target"] = np.where(up, m["sw_hi"], m["sw_lo"])
    m.loc[rng_ < F["min_swing_points"], ["z_near", "z_mid", "z_far", "target"]] = np.nan
    return m


def signals(c15: pd.DataFrame, R: dict | None = None) -> pd.DataFrame:
    R = R or rules()
    P, K, T = R["trigger"], R["risk"], R["time"]
    c = c15.reset_index(drop=True)
    O, H, L, C = msw._arr(c)
    ts = pd.to_datetime(c["timestamp"])
    days = ts.dt.date.values
    m = context(c, R)
    rows = []
    t_from = dtime.fromisoformat(T["entry_from"])
    for i in range(1, len(c) - 1):
        d = int(m.at[i, "dir"])
        if d == 0 or np.isnan(m.at[i, "z_far"]):
            continue
        near, far, tgt = m.at[i, "z_near"], m.at[i, "z_far"], m.at[i, "target"]
        rng = H[i] - L[i]
        body = abs(C[i] - O[i])
        if rng < P["min_range_points"]:
            continue
        if d > 0:
            wick = min(O[i], C[i]) - L[i]
            in_zone = L[i] <= near and L[i] >= far - P["zone_tolerance_points"]
            closed_ok = C[i] >= far
        else:
            wick = H[i] - max(O[i], C[i])
            in_zone = H[i] >= near and H[i] <= far + P["zone_tolerance_points"]
            closed_ok = C[i] <= far
        if not (in_zone and closed_ok and wick >= P["wick_body_ratio"] * body and wick >= P["wick_range_share"] * rng):
            continue
        entry = C[i]
        sl = (min(L[i], far) - K["sl_buffer_points"]) if d > 0 else (max(H[i], far) + K["sl_buffer_points"])
        risk = abs(entry - sl)
        reward = (tgt - entry) if d > 0 else (entry - tgt)
        if risk <= 0 or reward <= 0:
            continue
        rr = reward / risk
        end = i
        while end + 1 < len(c) and days[end + 1] == days[i]:
            end += 1
        res, k, r = msw._replay(H, L, C, "bull" if d > 0 else "bear", i, entry, sl, tgt, end - i)
        t_ = ts.iloc[i].time()
        rows.append({"idx": i, "time": ts.iloc[i], "side": "bull" if d > 0 else "bear", "entry": entry, "sl": sl,
                     "target": tgt, "rr": round(rr, 2), "risk": risk,
                     "passes": rr >= K["min_rr"] and risk >= K["min_sl_points"],
                     "in_volume_hours": t_ <= dtime.fromisoformat(T["morning_until"]) or
                                        dtime.fromisoformat(T["post_lunch_from"]) <= t_ <= dtime.fromisoformat(T["post_lunch_until"]),
                     "morning": t_ <= dtime.fromisoformat(T["morning_until"]),
                     "after_entry_from": t_ >= t_from, "result": res, "r": r,
                     "zone": f"{m.at[i, 'z_near']:,.0f}-{m.at[i, 'z_far']:,.0f}"})
    return pd.DataFrame(rows)


def _row(df, lab, minimum):
    n = len(df)
    if n < minimum:
        return {"Setup": lab, "Trades": n, "Win %": "INSUFFICIENT SAMPLE", "SL %": "", "Avg R": ""}
    return {"Setup": lab, "Trades": n, "Win %": round(100 * (df["result"] == "TARGET").mean(), 1),
            "SL %": round(100 * (df["result"] == "SL").mean(), 1), "Avg R": round(float(df["r"].mean()), 2)}


def backtest(R: dict | None = None) -> dict:
    R = R or rules()
    H = R["history"]
    h = ms.load_history(ms.rules())
    if h is None or len(h) < 2000:
        return {"error": "no 15-min history (Dhan token?)"}
    h = h.reset_index(drop=True)
    real = signals(h, R)
    ctrl = signals(msw._shuffle(h, 42, k=H["control_shuffles"]), R)
    t = real[real["passes"]] if len(real) else real
    ct = ctrl[ctrl["passes"]] if len(ctrl) else ctrl
    mn = H["min_sample_size"]
    rows = [_row(t, "ALL: direction + discount/premium zone + pin bar, R:R >= %.1f" % R["risk"]["min_rr"], mn),
            _row(ct, f"CONTROL: shuffled bars ({H['control_shuffles']} pooled)", mn)]
    for lab, f in (("  buys (uptrend, discount)", lambda x: x["side"] == "bull"),
                   ("  sells (downtrend, premium)", lambda x: x["side"] == "bear"),
                   ("  in his volume hours (09:15-11:00 + post-lunch)", lambda x: x["in_volume_hours"]),
                   ("  first 1.5 hours only (09:15-11:00)", lambda x: x["morning"]),
                   ("  outside volume hours (dead volume)", lambda x: ~x["in_volume_hours"])):
        rows.append(_row(t[f(t)] if len(t) else t, lab, mn))
        if "volume hours" in lab or "1.5 hours" in lab:
            rows.append(_row(ct[f(ct)] if len(ct) else ct, "    CONTROL, same filter", mn))
    rows.append(_row(real[~real["passes"]] if len(real) else real, "  (R:R below minimum - not taken)", mn))
    v = {}
    for key, lab, f in (("all", "All trades", lambda x: x["r"] == x["r"]),
                        ("volume_hours", "In his volume hours", lambda x: x["in_volume_hours"]),
                        ("morning", "First 1.5 hours", lambda x: x["morning"])):
        a, b = (t[f(t)] if len(t) else t), (ct[f(ct)] if len(ct) else ct)
        if len(a) < mn or len(b) < mn:
            v[key] = {"rule": lab, "verdict": "NOT ENOUGH DATA", "level": 0, "n": len(a), "detail": f"{len(a)} trades"}
            continue
        d = a["r"].mean() - b["r"].mean()
        lvl = 1 if d >= H["beats_by_r"] and a["r"].mean() > 0 else -1 if d <= -H["beats_by_r"] else 0
        small = lvl > 0 and len(a) < H["min_trades_for_star"]
        v[key] = {"rule": lab, "verdict": "PROMISING, SMALL SAMPLE" if small else
                  {1: "BEATS RANDOM", 0: "NO EDGE", -1: "WORSE THAN RANDOM"}[lvl], "level": 0 if small else lvl,
                  "n": int(len(a)), "detail": f"{a['r'].mean():+.2f} R vs {b['r'].mean():+.2f} R on shuffled bars, "
                                              f"win {100 * (a['result'] == 'TARGET').mean():.0f}%, {len(a)} trades"}
    return {"table": pd.DataFrame(rows), "verdicts": v, "span": (h["timestamp"].min(), h["timestamp"].max(), len(h))}


def save_verdict(v: dict) -> None:
    VERDICT_PATH.parent.mkdir(parents=True, exist_ok=True)
    VERDICT_PATH.write_text(json.dumps({"run": str(date.today()), **v}, indent=1))


def load_verdict() -> dict:
    try:
        return json.loads(VERDICT_PATH.read_text())
    except (OSError, ValueError):
        return {}


def live(now=None) -> dict:
    """Current direction, zone and whether the latest CLOSED 15-min candle is a trigger."""
    R = rules()
    c, src = ms.recent_candles(10)
    if c is None or len(c) < 40:
        return {"error": "no candles"}
    now = pd.Timestamp(now or pd.Timestamp.now())
    c = c[pd.to_datetime(c["timestamp"]) + pd.Timedelta(minutes=15) <= now].reset_index(drop=True)
    m = context(c, R)
    last = m.iloc[-1]
    out = {"dir": int(last["dir"]), "zone": None, "trigger": None, "source": src}
    if not np.isnan(last["z_far"]):
        out["zone"] = {"near": float(last["z_near"]), "mid": float(last["z_mid"]), "far": float(last["z_far"]),
                       "target": float(last["target"]), "swing": (float(last["sw_lo"]), float(last["sw_hi"]))}
    s = signals(c, R)
    if len(s):
        s = s[(s["idx"] == len(c) - 1) & s["passes"]]
        if len(s):
            out["trigger"] = s.iloc[-1].to_dict()
    return out


def text(L: dict) -> str:
    d = {1: "UP (buy only, in discount)", -1: "DOWN (sell only, in premium)", 0: "undecided - no trade"}[L["dir"]]
    msg = [f"Direction (1-hour structure): {d}"]
    z = L.get("zone")
    if z:
        kind = "DISCOUNT buy zone" if L["dir"] > 0 else "PREMIUM sell zone"
        msg.append(f"{kind} 0.705-0.886: {z['near']:,.0f} - {z['far']:,.0f} (0.79 = {z['mid']:,.0f}); "
                   f"a close beyond {z['far']:,.0f} = no trade; target {z['target']:,.0f}")
    x = L.get("trigger")
    if x:
        msg.append(f"TRIGGER: 15-min pin bar in the zone -> {'BUY CE' if x['side'] == 'bull' else 'BUY PE'} at "
                   f"{x['entry']:,.0f}, SL {x['sl']:,.0f}, target {x['target']:,.0f} (R:R {x['rr']:.1f})"
                   + ("" if x["in_volume_hours"] else " [outside his volume hours]"))
    return "\n".join(msg)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
    pd.set_option("display.width", 200, "display.max_columns", 12, "display.max_colwidth", 70)
    if a.backtest:
        b = backtest()
        if "error" in b:
            print(b["error"])
            return 1
        f, t, n = b["span"]
        print(f"15-min: {n:,} bars {f:%d-%b-%Y} .. {t:%d-%b-%Y} (1-hour built from them)")
        print(b["table"].to_string(index=False))
        print("\n== verdicts ==")
        for x in b["verdicts"].values():
            print(f"{x['verdict']:26} {x['rule']}: {x['detail']}")
        save_verdict(b["verdicts"])
        print(f"saved {VERDICT_PATH}")
        return 0
    L = live()
    print(L.get("error") or text(L))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
