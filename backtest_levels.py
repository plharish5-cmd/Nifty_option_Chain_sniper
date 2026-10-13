"""backtest_levels.py v2 — replay the 🎯 LEVELS break signals over every stored day (read-only, no orders).

Entry  = close of the candle that prints ⬆/⬇ BROKEN (BUY CE / BUY PE), 09:30–15:00.
Exit   = break failed / stop hit (close beyond SL), or 15:25.
Compares:  A) all breaks   B) breaks WITH the day trend (levels_read.TREND_FILTER)
Reports index points AND an estimated option-premium result (ATM option: delta 0.5, theta, spread + charges).
Usage:  python3 backtest_levels.py        python3 backtest_levels.py 20   (last 20 days)
"""
import sys
from datetime import datetime, time
import pandas as pd
import options_lab_tab as ol, store, pe_ce_panel as pc, levels_read as lr

DELTA, THETA_PER_HR, COST_PTS, LOT = 0.5, 6.0, 4.0, 65   # COST = spread/slippage both legs + Dhan charges ≈ ₹70/lot

def premium(pts, t_in, t_out):
    hrs = (datetime.strptime(t_out, "%H:%M") - datetime.strptime(t_in, "%H:%M")).seconds / 3600
    return DELTA * pts - THETA_PER_HR * hrs - COST_PTS

def trades(t):
    out, pos = [], None
    for x in t.itertuples():
        tm = x.ts.time()
        ev = x.lv[2] if isinstance(x.lv, tuple) else ""
        if pos and (ev == "fail" or tm >= time(15, 25)):
            pnl = (x.close - pos[1]) * (1 if pos[0] == "CE" else -1)
            out.append(dict(side=pos[0], entry_t=pos[2], entry=round(pos[1], 1), exit_t=f"{x.ts:%H:%M}", exit=round(x.close, 1),
                            pts=round(pnl, 1), why="stop" if ev == "fail" else "15:25"))
            pos = None
        if not pos and ev in ("break_up", "break_dn") and time(9, 30) <= tm < time(15, 0):
            pos = ("CE" if ev == "break_up" else "PE", x.close, f"{x.ts:%H:%M}")
    if pos:
        x = t.iloc[-1]; pnl = (x.close - pos[1]) * (1 if pos[0] == "CE" else -1)
        out.append(dict(side=pos[0], entry_t=pos[2], entry=round(pos[1], 1), exit_t=f"{x.ts:%H:%M}", exit=round(x.close, 1),
                        pts=round(pnl, 1), why="last"))
    return out

def run(days, filt):
    lr.TREND_FILTER, lr.TRAIL = filt, False
    r = pc._rules(); rows = []
    for d in days:
        try:
            snaps = store.load_day(d)
            if len(snaps) < 30: continue
            win = pc.strike_window(snaps, "Auto ATM", 10, None, r["strike_step"])
            t = pc.table_rows(pc.per_snapshot(snaps, win, r["oi_multiplier"]), 3)
            t = t[t.ts.dt.time < time(15, 30)]
            for tr in trades(t): rows.append(dict(day=str(d), **tr))
        except Exception as e:
            print(f"  skip {d}: {e}")
    df = pd.DataFrame(rows)
    if len(df):
        df["prem"] = [round(premium(p, a, b), 1) for p, a, b in zip(df.pts, df.entry_t, df.exit_t)]
    return df

def line(df, name):
    if df.empty: return f"{name:14} no trades"
    w, l = df[df.pts > 0], df[df.pts <= 0]
    pf = w.pts.sum() / abs(l.pts.sum()) if len(l) and l.pts.sum() else float("inf")
    return (f"{name:14} n {len(df):3} | win {len(w)/len(df):4.0%} | pts {df.pts.sum():+7.1f} (avg {df.pts.mean():+5.1f}) | PF {pf:4.2f} | "
            f"worst {df.pts.min():+6.1f} | est. premium {df.prem.sum():+7.1f} pts ≈ ₹{df.prem.sum()*LOT:+,.0f}/lot")

def report(df, name):
    print(f"\n── {name} ──")
    if df.empty: print("no trades"); return None
    print(line(df, "ALL"))
    for s in ("CE", "PE"): print(line(df[df.side == s], f"  {s} only"))
    best = df.groupby("day").pts.sum().idxmax()
    print(line(df[df.day != best], f"  w/o best day"), f"(best = {best})")
    return df

if __name__ == "__main__":
    days = sorted(ol._days_available())
    if len(sys.argv) > 1: days = days[-int(sys.argv[1]):]
    print(f"{len(days)} stored days: {days[0]} … {days[-1]}  (premium = {DELTA}×pts − {THETA_PER_HR} pts/hr theta − {COST_PTS} pts costs; an ESTIMATE)")
    a, b = run(days, False), run(days, True)
    report(a, "A · all breaks")
    report(b, "B · with day trend only")
    if len(b): print("\nB trades:\n" + b.to_string(index=False))
    print()
    ok = (len(b) >= 20 and b.pts.sum() > 0 and b.prem.sum() > 0
          and all(b[b.side == s].pts.sum() > 0 for s in ("CE", "PE") if (b.side == s).any())
          and b[b.day != b.groupby("day").pts.sum().idxmax()].prem.sum() > 0)
    if len(b) < 20:
        print(f"VERDICT: only {len(b)} filtered trades — too few. Paper-trade it, collect more days.")
    elif ok:
        print("VERDICT: trend-filtered breaks hold up (both sides positive, survive removing the best day, premium positive). "
              "OK to trade SMALL (1 lot) — keep TREND_FILTER = True.")
    else:
        print("VERDICT: not robust yet (a side, the premium estimate, or the result without the best day is negative). "
              "Keep it as information / paper trades.")
    lr.TREND_FILTER = True
