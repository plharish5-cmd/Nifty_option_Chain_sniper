"""
replay_day.py — run EVERY mentor rule on one day's 15-min candles and show what each would have signalled,
with entry / SL / target and what happened by the close. Uses the same code as the live alerts.

  .venv/bin/python replay_day.py                 # today
  .venv/bin/python replay_day.py 2026-09-28      # any day in the cached 1-year 15-min history
"""
from __future__ import annotations

import sys
from datetime import date

import pandas as pd

import mentor_structure as ms
import mentor_sweeps as msw


def _t(c, i):
    return f"{pd.Timestamp(c['timestamp'].iloc[int(i)]):%H:%M}"


def _candles_from_snapshots(day) -> pd.DataFrame | None:
    """15-min OHLC of the spot price stored with each option-chain snapshot (fallback when Dhan candles are missing)."""
    try:
        import store
        s = store.load_day(day)
    except Exception as exc:
        print(f"(snapshots unavailable: {exc})")
        return None
    rows = [(pd.Timestamp(x[0]), float(x[1])) for x in s or [] if x[1] is not None]
    if len(rows) < 10:
        return None
    p = pd.DataFrame(rows, columns=["t", "spot"]).set_index("t").sort_index()
    p = p.between_time("09:15", "15:29")
    o = p["spot"].resample("15min", origin="start_day", offset="9h15min").ohlc().dropna()
    o = o.reset_index().rename(columns={"t": "timestamp"})
    o.attrs["n"] = len(p)
    return o


def main() -> int:
    day = pd.Timestamp(sys.argv[1]).date() if len(sys.argv) > 1 else date.today()
    h = ms.load_history(ms.rules(), refresh=True)
    if h is None:
        print("no 15-min history (Dhan token?)")
        return 1
    h = h.reset_index(drop=True)
    d = h["timestamp"].dt.date
    if day not in set(d):
        snap = _candles_from_snapshots(day)
        if snap is None:
            print(f"{day} not in the 15-min history (last day {d.max()}) and no option-chain snapshots for it")
            return 1
        print(f"{day} not in the Dhan 15-min history - built {len(snap)} candles from {snap.attrs['n']} "
              f"option-chain snapshots (spot sampled ~1/min, so wicks are approximate)")
        tz = h["timestamp"].dt.tz
        if tz is not None and snap["timestamp"].dt.tz is None:
            snap["timestamp"] = snap["timestamp"].dt.tz_localize(tz)
        h = pd.concat([h[d < day], snap], ignore_index=True)
        d = h["timestamp"].dt.date
    # 10 sessions of context before the day, nothing after it
    days = sorted(set(d[d <= day]))[-11:]
    c = h[d.isin(days)].reset_index(drop=True)
    today = c["timestamp"].dt.date == day
    first = int(today.idxmax())
    V = msw.load_verdicts().get("rules", {})
    OB = ms.load_verdict()
    rows = []

    def add(rule, verdict, i, side, entry, sl, target, result, note=""):
        rows.append({"Time": _t(c, i), "Rule": rule, "Side": side, "Entry": round(entry) if entry is not None else "",
                     "SL": round(sl) if sl is not None else "", "Target": round(target) if target is not None else "",
                     "By the close": result, "Backtest": verdict, "Note": note})

    def vtxt(key):
        x = V.get(key)
        return x["verdict"] if x else "not tested"

    # 1. swing + candle sweeps (Lecture 7)
    sw = msw.sweep_signals(c)
    for _, x in sw[sw["idx"] >= first].iterrows():
        if x["kind"] == "swing" or x["passes_rr"]:
            add(f"{x['kind'].title()} sweep", vtxt(f"{x['kind']}_sweep_15m"), x["idx"],
                "BUY" if x["side"] == "bull" else "SELL", x["entry"], x["sl"], x["target"], x["result"],
                ("" if x["passes_rr"] else "R:R < 1 - he skips"))
    # 2. body voids (Lecture 6)
    bv = msw.body_voids(c)
    for _, x in bv[bv["entry"].notna()].iterrows():
        if x["touch_idx"] >= first:
            add("Body void rejection", vtxt("body_void_15m"), x["touch_idx"], "BUY" if x["side"] == "bull" else "SELL",
                x["entry"], x["sl"], x["target"], x["result"], "" if x["passes_rr"] else "R:R < 1 - he skips")
    # 3. two-candle double bottom / top (Lecture 7)
    dz = msw.double_zones(c)
    for _, x in dz[dz["formed_idx"] >= first].iterrows():
        add("Double bottom/top zone", vtxt("double_15m"), x["formed_idx"], "BUY" if x["side"] == "bull" else "SELL",
            x["entry"] if pd.notna(x["entry"]) else None, x["sl"] if pd.notna(x["sl"]) else None, x["target"],
            x["result"] or x["status"], x["status"])
    # 4. structure events + order blocks (Lectures 5-6)
    res = ms.analyse(c)
    ms.simulate(c, res)
    for e in res.events:
        if e["idx"] >= first and e["kind"] in ("BOS_UP", "BOS_DN", "CHOCH_UP", "CHOCH_DN", "SHIFT"):
            add(e["kind"].replace("_", " "), "structure event", e["idx"], "", None, None, None, f"level {e['price']:,.0f}")
    for o in res.obs:
        if o.valid_from >= first:
            add(f"Order block ({o.rank})", OB.get("verdict", "?"), o.valid_from, "BUY" if o.side == "bull" else "SELL",
                o.zone_hi if o.side == "bull" else o.zone_lo, o.sl, o.t1,
                o.result or ("WAITING" if o.tap_idx is None else "TAPPED"),
                "he deletes MIDDLE ones" if o.rank == "MIDDLE" else "")
    # 5. swing range at the open (Lecture 7)
    sr = msw.swing_range_days(c)
    if len(sr) and sr.iloc[-1]["day"] == day:
        r = sr.iloc[-1]
        add("Swing range at the open", vtxt("useless_open"), first, "", None, None, None,
            f"opened {r['open_is']} ({r['swing_low']:,.0f}-{r['swing_high']:,.0f})"
            + (f"; broke {r['break']} at {pd.Timestamp(r['break_time']):%H:%M} -> {r['trade']}" if r["break"] else ""))
    # 6. RSI divergences (final class)
    try:
        import mentor_extras as mx
        dv = mx.divergences(c)
        for _, x in dv[dv["idx"] >= first].iterrows() if len(dv) else []:
            add(x["kind"], "no edge in backtest", x["idx"], "BUY" if x["side"] == "bull" else "SELL",
                x["entry"], x["sl"], x["target"], x["result"])
    except Exception as exc:
        print(f"(divergences skipped: {exc})")

    # 7. Fib 1.2/1.4 after breaks + latest-imbalance hold (Fibo + Doubts class)
    try:
        import mentor_fib as mf
        vf = mf.load_verdict()
        ft = mf.fib_targets(c)
        for _, x in ft[ft["idx"] >= first].iterrows() if len(ft) else []:
            add(f"Fib after {x['kind'].replace('_', ' ')}", "see mentor_fib backtest", x["idx"],
                "BUY" if x["side"] == "bull" else "SELL", None, None, x["t1.2"],
                "1.2 HIT" if x["hit1.2"] else "1.2 not reached", f"1.4 = {x['t1.4']:,.0f}")
        ih = mf.imbalance_hold(c)
        for _, x in ih[(ih["touch_idx"] >= first) & ih["entry"].notna()].iterrows() if len(ih) else []:
            add("Imbalance hold", vf, x["touch_idx"], "BUY" if x["side"] == "bull" else "SELL",
                x["entry"], x["sl"], x["target"], x["result"], "" if x["passes_rr"] else "R:R < 1 - he skips")
    except Exception as exc:
        print(f"(fib/imbalance skipped: {exc})")

    # 8. rejection wick at a support / resistance level + follow-through (mentor_levels.py)
    try:
        import mentor_levels as ml
        LV = ml.load_verdict().get("15m", {})
        daily_lv = None
        try:
            import participant_correlation as pc
            dd = msw.daily_frame(pc.load_ohlc()).rename(columns={"timestamp": "date"})
            dd["date"] = pd.to_datetime(dd["date"]).dt.date
            daily_lv = dd[dd["date"] < day].reset_index(drop=True)
        except Exception as exc:
            print(f"(daily levels from the 15-min history only: {exc})")
        sl_ = ml.signals(c, tf="15m", daily=daily_lv, extra=ml.manual_levels())
        for _, x in sl_[(sl_["idx"] >= first) & sl_["confirmed"]].iterrows() if len(sl_) else []:
            add(f"Rejection at {x['level_type'].replace('_', ' ')} {x['level']:,.0f}",
                LV.get(x["level_type"], {}).get("verdict", "not tested"), x["idx"] + 1,
                "BUY" if x["side"] == "bull" else "SELL", x["entry"], x["sl"], x["target"],
                f"{x['exit_first']} ({x['r_first']:+.1f} R)", "" if x["passes"] else "SL below minimum - skip")
    except Exception as exc:
        print(f"(rejection-at-level skipped: {exc})")

    t = pd.DataFrame(rows)
    ct = c[today]
    print(f"\n{day:%a %d-%b-%Y}  open {ct['open'].iloc[0]:,.1f}  high {ct['high'].max():,.1f}  low {ct['low'].min():,.1f}"
          f"  close {ct['close'].iloc[-1]:,.1f}  ({len(ct)} x 15-min candles, index)")
    try:
        import participant_correlation as pc
        dd = pc.load_ohlc()
        if dd is not None:
            daily = msw.daily_frame(dd)
            daily = daily[pd.to_datetime(daily["timestamp"]).dt.date <= day]
            x = msw._last_candle_read(daily.reset_index(drop=True), "day")
            if x:
                print(f"Day candle: {x['text']}")
            print(f"Day structure (as of the previous close): "
                  f"{ {1: 'BULLISH', -1: 'BEARISH', 0: 'undecided'}[int(msw.day_bias(daily).iloc[-1])] }")
    except Exception as exc:
        print(f"(day chart skipped: {exc})")
    if not len(t):
        print("No mentor signal today.")
        return 0
    t = t.drop_duplicates(["Time", "Rule", "Side", "Entry", "SL", "Target"]).sort_values("Time", kind="stable")
    pd.set_option("display.width", 250, "display.max_columns", 20, "display.max_colwidth", 40)
    print(t.to_string(index=False))
    main_ = t[t["Backtest"].eq("BEATS RANDOM") & t["Entry"].ne("") & t["Note"].eq("")]
    if len(main_):
        won = (main_["By the close"] == "TARGET").sum()
        lost = (main_["By the close"] == "SL").sum()
        print(f"\nRules that beat random in the backtest: {len(main_)} signals today -> {won} target, {lost} SL, "
              f"{len(main_) - won - lost} still open / timed out.")
    out = f"data/replay_{day}.csv"
    t.to_csv(out, index=False)
    print(f"saved {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
