"""
homework.py — your daily homework trades vs what the app found on the same day.

  .venv/bin/python homework.py 2026-09-30        # one day
  .venv/bin/python homework.py                   # every day in data/homework/trades.csv

data/homework/trades.csv  (one row per trade you marked; add each day's homework here, or send me the chart):
  date,entry_time,side,setup,level,points,traded,notes
  2026-09-30,10:30,PE,inverted hammer,day high (R1),40,no,...

8 EMA rows (setup contains 'ema', no 'hammer') are matched against ema8_signals.py (15-min).
A homework trade counts as FOUND when the app has a TRIGGERED setup on the same side whose entry candle is within
match_minutes of your entry time. The score to watch over the weeks: found % (does the app see what you see)
and how many extra setups it raises that you did not take (noise).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

import ema8_signals as e8
import pa_signals as pa

HERE = Path(__file__).resolve().parent
HW = HERE / "data" / "homework" / "trades.csv"
MATCH_MINUTES = 15


def score(day) -> tuple[pd.DataFrame, pd.DataFrame]:
    hw = pd.read_csv(HW)
    hw = hw[pd.to_datetime(hw["date"]).dt.date == day].copy()
    c = pa.day_candles(day)
    s = pa.scan(c, pa.rules(), only_day=day) if c is not None else pd.DataFrame()
    trig = s[s["triggered"]].copy() if len(s) else s
    e = e8.scan(c, e8.rules_for("15m"), only_day=day) if c is not None else pd.DataFrame()
    etrig = e[e["triggered"]].copy() if len(e) else e
    rows = []
    used, eused = set(), set()
    for _, h in hw.iterrows():
        t = pd.Timestamp(f"{h['date']} {h['entry_time']}")
        want = str(h["setup"]).lower()
        if ("ema" in want or "stage" in want) and "hammer" not in want:          # your 8 EMA trades -> ema8_signals.py
            best = None
            for k, x in etrig.iterrows() if len(etrig) else []:
                if x["side"] != h["side"] or k in eused:
                    continue
                dt = min(abs((x["time"] - t).total_seconds()), abs((x["trigger_time"] - t).total_seconds())) / 60
                if dt <= MATCH_MINUTES and (best is None or dt < best[1]):
                    best = (k, dt)
            lab = f"{h['entry_time']} {h['side']} {h['setup']} ({h['points']:+})"
            if best:
                eused.add(best[0])
                x = etrig.loc[best[0]]
                rows.append({"Your trade": lab, "App": f"FOUND 8 EMA {x['time']:%H:%M} close, entry {x['trigger_time']:%H:%M} "
                             f"{x['entry']:,.0f} SL {x['sl']:,.0f} ({x['risk']:.0f}); TSL {x['r_trail'] * x['risk']:+.0f} "
                             f"mix {x['r_mix'] * x['risk']:+.0f} ({x['exit_mix']})"})
            else:
                near = e[e["side"] == h["side"]] if len(e) else e
                why = "no 8 EMA close on that side near that time"
                if len(near):
                    d = (near["time"] - t).abs().dt.total_seconds() / 60
                    x = near.loc[d.idxmin()]
                    if best is None and any(abs((etrig.loc[q, "time"] - t).total_seconds()) / 60 <= MATCH_MINUTES + 15
                                            and etrig.loc[q, "side"] == h["side"] for q in eused):
                        why = "same app trade as your previous row (two charts of one trade?)"
                    elif d.min() <= MATCH_MINUTES + 30:
                        why = (f"nearest 8 EMA close {x['time']:%H:%M}: " + (x["skip"] if isinstance(x["skip"], str)
                               else "no follow-through" if not x["triggered"] else f"triggered {x['trigger_time']:%H:%M}, too far from yours"))
                rows.append({"Your trade": lab, "App": "MISSED - " + why})
            continue
        best = None
        for k, x in trig.iterrows() if len(trig) else []:
            if x["side"] != h["side"] or k in used:
                continue
            # same pattern family: your 'double hammer' must be matched by a double hammer, 'inverted' by an IH
            if ("double" in want) != (x["pattern"] == "double hammer") or ("inverted" in want) != (x["pattern"] == "inverted hammer"):
                continue
            dt = abs((pd.Timestamp(x["trigger_time"]) - t).total_seconds()) / 60
            if dt <= MATCH_MINUTES and (best is None or dt < best[1]):
                best = (k, dt)
        if best:
            used.add(best[0])
            x = trig.loc[best[0]]
            rows.append({"Your trade": f"{h['entry_time']} {h['side']} {h['setup']} at {h['level']} (+{h['points']})",
                         "App": f"FOUND {pd.Timestamp(x['trigger_time']):%H:%M} {x['pattern']} at {x['levels']} "
                                f"entry {x['entry']:,.0f} SL {x['sl']:,.0f}; trail {x['r_trail'] * x['risk']:+.0f} pts"})
        else:
            near = s[(s["side"] == h["side"])] if len(s) else s
            why = "no hammer / IH at a level near that time"
            if len(near):
                d = (pd.to_datetime(near["time"]) - t).abs().dt.total_seconds() / 60
                if d.min() <= MATCH_MINUTES + 15:
                    x = near.loc[d.idxmin()]
                    if x["triggered"]:
                        why = (f"app saw a {x['pattern']} at {pd.Timestamp(x['time']):%H:%M} ({x['levels']}), triggered "
                               f"{pd.Timestamp(x['trigger_time']):%H:%M} - different pattern or more than {MATCH_MINUTES} min from yours")
                    else:
                        why = (f"setup seen at {pd.Timestamp(x['time']):%H:%M} ({x['pattern']} at {x['levels']}) but "
                               + (x["skip"] if isinstance(x.get("skip"), str) else "no follow-through within the trigger window"))
            rows.append({"Your trade": f"{h['entry_time']} {h['side']} {h['setup']} at {h['level']} (+{h['points']})",
                         "App": "MISSED - " + why})
    extra = trig.drop(index=list(used)) if len(trig) else trig
    eextra = etrig.drop(index=list(eused)) if len(etrig) else etrig
    return pd.DataFrame(rows), extra, eextra


def main() -> int:
    pd.set_option("display.width", 250, "display.max_colwidth", 140)
    days = [pd.Timestamp(sys.argv[1]).date()] if len(sys.argv) > 1 else sorted(pd.to_datetime(pd.read_csv(HW)["date"]).dt.date.unique())
    tot = found = 0
    for day in days:
        t, extra, eextra = score(day)
        tot += len(t)
        found += int(t["App"].str.startswith("FOUND").sum()) if len(t) else 0
        print(f"\n=== {day:%a %d-%b-%Y} ===")
        print(t.to_string(index=False) if len(t) else "no homework rows for this day")
        if len(extra):
            print(f"\nApp setups you did not mark ({len(extra)}):")
            for _, x in extra.iterrows():
                print(f"  {pd.Timestamp(x['trigger_time']):%H:%M} {x['side']} {x['pattern']} at {x['levels']} -> trail "
                      f"{x['r_trail'] * x['risk']:+.0f} pts")
        if len(eextra):
            print(f"\n8 EMA trades the app took that you did not mark ({len(eextra)}):")
            for _, x in eextra.iterrows():
                print(f"  {x['trigger_time']:%H:%M} {x['side']} entry {x['entry']:,.0f} SL {x['sl']:,.0f} -> mix "
                      f"{x['r_mix'] * x['risk']:+.0f} pts ({x['exit_mix']})")
    if tot:
        print(f"\nFound {found} of {tot} homework trades ({100 * found / tot:.0f}%).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
