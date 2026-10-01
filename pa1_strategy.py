"""
pa1_strategy.py — PRICE ACTION STRATEGY 1 (MJ / Sushen notes v4.1 + Harish's own rules), 15-min NIFTY index.

  BIAS (checked first)     UBB sloping UP = buys only · LBB sloping DOWN = sells only · both bands flat or
                           BB width < rangebound_width = STAGE 1 rangebound -> no 8 EMA trades
  SETUP                    candle CLOSES above (buy) / below (sell) the 8 EMA  — not the first 15-min candle
  FOLLOW-THROUGH           the NEXT candle is the same colour and closes beyond the signal candle's close -> enter
                           at that candle's close (Sushen rule 2)
  SL                       signal candle LOW (buy) / HIGH (sell), must be <= max_sl (Sushen rule 3: close to the EMA)
  LOCATION (tag + test)    signal near PDH / PDL / PDC, previous days' wick highs / lows, pivots R1-R4 / S1-S4
  RULES                    one trade per rally (rule 1) · no fresh trades after no_new_after (13:00, low volume)
                           · max trades per day · stop after 2 losses (rangebound signal)
  EXITS — three columns for every trade
    +15 exit   book at +15, else SL / 15:15
    +20 exit   book at +20, else SL / 15:15
    Trail exit SL trails every candle low / high, SL to cost at +20, exit at the next level on the way (PDH / PDL /
               PDC / R1-R4 / S1-S4) or doji at day high / low, max +120, out 15:15

  .venv/bin/python pa1_strategy.py              # 5-year test + journal + report
  .venv/bin/python pa1_strategy.py --day 2026-09-30

  Output data/ema8_journal/pa1/:  pa1_journal.csv (every trade, your homework columns + 3 exit columns),
  pa1_summary_by_year.csv, pa1_report.xlsx (Rule ladder · By year · Trades). Numbers in ema8_signals_rules.json -> "pa1".
Information only - not financial advice.
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

import numpy as np
import pandas as pd

import ema8_signals as e8

log = logging.getLogger("pa1")
HERE = Path(__file__).resolve().parent
OUT = HERE / "data" / "ema8_journal" / "pa1"

DEFAULT = {"bb_slope_bars": 3, "bb_slope_min_pts": 3, "rangebound_width": 60, "skip_first_candle": True,
           "max_sl": 40, "min_sl": 5, "no_new_after": "13:00", "max_trades_per_day": 3, "max_losses_per_day": 2,
           "one_trade_per_rally": True, "level_near_pts": 20, "wick_days": 3, "wick_share": 0.3,
           "t1": 15, "t2": 20, "cost_to_cost_at": 20, "max_pts": 120, "level_min_gap": 15, "doji_share": 0.15,
           "sl_mode": "signal", "mixed_by_mid": True, "cost_points_per_trade": 2.5, "train_until": "2024-12-31", "exit_by": "15:15"}


def prules() -> dict:
    p = dict(DEFAULT)
    p.update({k: v for k, v in e8.rules().get("pa1", {}).items() if not k.startswith("_")})
    return p


# ─────────────────────────────── levels per day ─────────────────────────────
def day_levels(c: pd.DataFrame, P: dict) -> dict:
    """{day: {name: price}} from PREVIOUS days only: PDH/PDL/PDC, floor pivots R1-R4/S1-S4, wick highs/lows."""
    ts = pd.to_datetime(c["timestamp"])
    d = c.assign(day=ts.dt.date).groupby("day").agg(o=("open", "first"), h=("high", "max"), l=("low", "min"),
                                                     cl=("close", "last"))
    days = list(d.index)
    out = {}
    for k in range(1, len(days)):
        y = d.iloc[k - 1]
        H, L, C = y.h, y.l, y.cl
        p = (H + L + C) / 3
        r1, s1 = 2 * p - L, 2 * p - H
        r2, s2 = p + (H - L), p - (H - L)
        r3, s3 = H + 2 * (p - L), L - 2 * (H - p)
        lv = {"PDH": H, "PDL": L, "PDC": C, "R1": r1, "S1": s1, "R2": r2, "S2": s2, "R3": r3, "S3": s3,
              "R4": r3 + (r2 - r1), "S4": s3 - (s1 - s2)}
        for b in range(1, P["wick_days"] + 1):             # long wicks of the last few days
            if k - b < 0:
                break
            z = d.iloc[k - b]
            rng = max(z.h - z.l, 1)
            if (z.h - max(z.o, z.cl)) / rng >= P["wick_share"]:
                lv[f"wick high {days[k - b]:%d-%b}"] = z.h
            if (min(z.o, z.cl) - z.l) / rng >= P["wick_share"]:
                lv[f"wick low {days[k - b]:%d-%b}"] = z.l
        out[days[k]] = lv
    return out


# ─────────────────────────────── scan ───────────────────────────────────────
def scan(c: pd.DataFrame, P: dict, only_day=None) -> pd.DataFrame:
    R = e8.rules_for("15m")
    a = e8._arrays(c, R)
    O, H, L, C, ema, ema4, days, tod, n = (a[k] for k in ("O", "H", "L", "C", "ema", "ema4", "days", "tod", "n"))
    ubb, lbb = a["bbu"], a["bbl"]
    lvls = day_levels(c, P)
    k_ = P["bb_slope_bars"]
    rows = []
    for i in range(k_ + 1, n - 1):
        d = days[i]
        if only_day is not None and d != only_day:
            continue
        if d not in lvls or days[i + 1] != d:
            continue
        if P["skip_first_candle"] and tod[i] < "09:30":
            continue
        up = C[i] > ema[i] and C[i - 1] <= ema[i - 1] and C[i] > O[i]
        dn = C[i] < ema[i] and C[i - 1] >= ema[i - 1] and C[i] < O[i]
        # Stage 2 / 3: already on that side, candle TOUCHES the 8 EMA and closes away from it
        s2 = C[i] > ema[i] and C[i - 1] > ema[i - 1] and L[i] <= ema[i] and C[i] > O[i]
        s3 = C[i] < ema[i] and C[i - 1] < ema[i - 1] and H[i] >= ema[i] and C[i] < O[i]
        if not (up or dn or s2 or s3):
            continue
        side = "CE" if (up or s2) else "PE"
        kind = "8 EMA touch (Stage %s)" % ("2" if s2 else "3") if (s2 or s3) else "close %s 8 EMA" % ("above" if up else "below")
        f = i + 1                                           # follow-through candle
        ft = (C[f] > O[f] and C[f] > C[i]) if side == "CE" else (C[f] < O[f] and C[f] < C[i])
        us, ls = ubb[i] - ubb[i - k_], lbb[i] - lbb[i - k_]
        width = ubb[i] - lbb[i]
        bias = ("rangebound" if (width < P["rangebound_width"] or (abs(us) < P["bb_slope_min_pts"] and abs(ls) < P["bb_slope_min_pts"]))
                else "up" if us >= P["bb_slope_min_pts"] else "down" if ls <= -P["bb_slope_min_pts"] else "mixed")
        entry = C[f]
        if P.get("sl_mode", "signal") == "follow":       # tighter: the follow-through (entry) candle's low / high
            sl = (L[f] - 1) if side == "CE" else (H[f] + 1)
        else:                                            # the signal candle's low / high
            sl = (L[i] - 1) if side == "CE" else (H[i] + 1)
        risk = abs(entry - sl)
        lv = lvls[d]
        px = L[i] if side == "CE" else H[i]
        near = [k for k, v in lv.items() if abs(v - px) <= P["level_near_pts"]]
        rows.append({"idx": i, "f": f, "day": d, "time": pd.Timestamp(a["ts"][i]), "entry_time": pd.Timestamp(a["ts"][f]),
                     "side": side, "kind": kind, "follow_through": ft, "bias": bias, "bb_width": width,
                     "bias_ok": ((bias == "up" and side == "CE") or (bias == "down" and side == "PE")
                                 or (P.get("mixed_by_mid", True) and bias == "mixed"
                                     and ((side == "CE") == (C[i] > (ubb[i] + lbb[i]) / 2)))),
                     "entry": entry, "sl": sl, "risk": risk, "sep": abs(ema4[i] - ema[i]),
                     "at_level": ", ".join(near), "levels": lv})
    return pd.DataFrame(rows), a


def _exits(a: dict, side: str, f: int, entry: float, sl: float, lv: dict, P: dict) -> dict:
    s = 1 if side == "CE" else -1
    O, H, L, C, days, tod, n = (a[k] for k in ("O", "H", "L", "C", "days", "tod", "n"))
    slip = 1.0
    end = f
    while end + 1 < n and days[end + 1] == days[f] and tod[end + 1] <= P["exit_by"]:
        end += 1
    # next level in the trade direction, at least level_min_gap away
    tgt = sorted([(v, k) for k, v in lv.items() if s * (v - entry) >= P["level_min_gap"]], key=lambda z: s * (z[0] - entry))
    out, mfe = {}, 0.0
    for col, fixed in (("t1", P["t1"]), ("t2", P["t2"]), ("trail", None)):
        stop, px, how, kx = sl, None, "15:15 day end", end
        day_hi, day_lo = H[f], L[f]
        tg = tgt[0] if (fixed is None and tgt) else None
        for k in range(f + 1, end + 1):
            kx = k
            if (L[k] <= stop) if s > 0 else (H[k] >= stop):
                px = (min(stop, O[k]) if s > 0 else max(stop, O[k])) - s * slip
                how = "SL hit" if stop == sl else ("cost to cost" if abs(stop - entry) < 0.01 else "trailing SL")
                break
            fav = (H[k] - entry) if s > 0 else (entry - L[k])
            if fixed is None:
                mfe = max(mfe, fav)
            if fixed is not None and fav >= fixed:
                px, how = entry + s * fixed, f"+{fixed} target"
                break
            if fixed is None:
                if tg is not None and ((H[k] >= tg[0]) if s > 0 else (L[k] <= tg[0])):
                    px, how = tg[0], f"at {tg[1]} {tg[0]:,.0f}"
                    break
                if fav >= P["max_pts"]:
                    px, how = entry + s * P["max_pts"], f"+{P['max_pts']} max"
                    break
                if fav >= P["cost_to_cost_at"]:
                    stop = max(stop, entry) if s > 0 else min(stop, entry)
                stop = max(stop, L[k] - 1) if s > 0 else min(stop, H[k] + 1)
                day_hi, day_lo = max(day_hi, H[k]), min(day_lo, L[k])
                rng = H[k] - L[k]
                at_ext = (H[k] >= day_hi) if s > 0 else (L[k] <= day_lo)
                if rng > 0 and abs(C[k] - O[k]) <= P["doji_share"] * rng and at_ext and s * (C[k] - entry) > 0:
                    px, how = C[k], "doji at day %s" % ("high" if s > 0 else "low")
                    break
        if px is None:
            px = C[end]
        out[col] = {"px": float(px), "t": pd.Timestamp(a["ts"][kx]), "how": how,
                    "pts": round(s * (px - entry) - P["cost_points_per_trade"], 1)}
    out["mfe"] = mfe
    return out


def trades(sig: pd.DataFrame, a: dict, P: dict, on: set[str]) -> pd.DataFrame:
    t = sig[sig["follow_through"]]
    t = t[(t["risk"] >= P["min_sl"])]
    if "max_sl" in on:
        t = t[t["risk"] <= P["max_sl"]]
    if "bias" in on:
        t = t[t["bias_ok"]]
    if "level" in on:
        t = t[t["at_level"] != ""]
    rows = []
    for d, g in t.sort_values("entry_time").groupby("day"):
        n_tr = n_loss = 0
        busy, last_side, rally_used = None, None, set()
        dsig = sig[sig["day"] == d].sort_values("time")
        for _, x in g.iterrows():
            if "no_late" in on and f"{x['entry_time']:%H:%M}" >= P["no_new_after"]:
                break
            if busy is not None and x["entry_time"] <= busy:
                continue
            if "day_limits" in on and (n_tr >= P["max_trades_per_day"] or n_loss >= P["max_losses_per_day"]):
                break
            if "one_rally" in on and last_side == x["side"]:
                # a new rally needs an opposite 8 EMA close after the last trade
                opp = dsig[(dsig["side"] != x["side"]) & (dsig["time"] > last_t) & (dsig["time"] < x["time"])]
                if not len(opp):
                    continue
            e = _exits(a, x["side"], int(x["f"]), x["entry"], x["sl"], x["levels"], P)
            busy = e["trail"]["t"]
            last_side, last_t = x["side"], x["time"]
            n_tr += 1
            n_loss += e["trail"]["pts"] < 0
            rows.append({**{k: v for k, v in x.items() if k != "levels"}, **{f"{c}_{k}": v for c in ("t1", "t2", "trail")
                                                                            for k, v in e[c].items()}, "mfe": e["mfe"]})
    return pd.DataFrame(rows)


def _stats(tr, P) -> dict:
    if not len(tr):
        return {"Trades": 0}
    yr = pd.to_datetime(tr["entry_time"]).dt.year
    cut = pd.Timestamp(P["train_until"]).year
    o = {}
    for lab, m in (("2021-24", yr <= cut), ("2025-26", yr > cut), ("5 yrs", yr > 0)):
        g = tr[m]
        o[f"{lab} trades"] = len(g)
        for c, nm in (("t1", "+15"), ("t2", "+20"), ("trail", "trail")):
            o[f"{lab} {nm} pts"] = round(g[f"{c}_pts"].sum()) if len(g) else 0
        o[f"{lab} trail win %"] = round(100 * (g["trail_pts"] > 0).mean(), 1) if len(g) else 0
    return o


def journal(tr: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for d, g in tr.sort_values("entry_time").groupby("day"):
        tots = {"t1": 0.0, "t2": 0.0, "trail": 0.0}
        for n_, (_, x) in enumerate(g.iterrows(), 1):
            for c in tots:
                tots[c] += x[f"{c}_pts"]
            buy = x["side"] == "CE"
            rows.append({
                "Date": f"{d:%d-%m-%Y}", "Trade #": n_, "Side": "BUY CE" if buy else "BUY PE",
                "Entry time": f"{x['entry_time']:%H:%M}", "Entry price": round(x["entry"], 1),
                "Entry reason": f"{x['time']:%H:%M} {x['kind']} + follow-through; bias {x['bias']}"
                                + (f"; at {x['at_level']}" if x["at_level"] else ""),
                "SL": round(x["sl"], 1), "Risk pts": round(x["risk"], 1),
                "+15 exit": f"{x['t1_t']:%H:%M} {x['t1_px']:,.0f} ({x['t1_how']})", "+15 pts": x["t1_pts"],
                "+20 exit": f"{x['t2_t']:%H:%M} {x['t2_px']:,.0f} ({x['t2_how']})", "+20 pts": x["t2_pts"],
                "Trail exit": f"{x['trail_t']:%H:%M} {x['trail_px']:,.0f} ({x['trail_how']})", "Trail pts": x["trail_pts"],
                "Max move pts": round(x["mfe"], 1),
                "Day +15": None, "Day +20": None, "Day trail": None})
        rows[-1].update({"Day +15": round(tots["t1"], 1), "Day +20": round(tots["t2"], 1), "Day trail": round(tots["trail"], 1)})
    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day")
    x = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    pd.set_option("display.width", 260, "display.max_columns", 30, "display.max_colwidth", 70)
    P = prules()
    h = e8._history("15m")
    if x.day:
        day = pd.Timestamp(x.day).date()
        c = e8.pa.tf_candles("15m", day, 10)
    else:
        c, day = h, None
    if c is None or not len(c):
        print("no 15-min candles")
        return 1
    c = c.reset_index(drop=True)
    print(f"scanning {len(c):,} 15-min candles ...")
    sig, a = scan(c, P, only_day=day)
    ALL = {"max_sl", "bias", "no_late", "one_rally", "day_limits"}
    if day:
        tr = trades(sig, a, P, ALL)
        j = journal(tr) if len(tr) else pd.DataFrame()
        print(j.to_string(index=False) if len(j) else "no Strategy-1 trade today")
        print(f"\n8 EMA closes today: {len(sig)}; with follow-through: {int(sig['follow_through'].sum()) if len(sig) else 0}")
        for _, s_ in sig.iterrows():
            print(f"  {s_['time']:%H:%M} {s_['side']} {s_['kind']:28} follow-through {'YES' if s_['follow_through'] else 'no ':3} "
                  f"bias {s_['bias']:10} SL {s_['risk']:.0f} {('at ' + s_['at_level']) if s_['at_level'] else ''}")
        return 0
    ladder = [("0. 8 EMA close + follow-through (no first candle)", set()),
              ("1. + SL <= %d" % P["max_sl"], {"max_sl"}),
              ("2. + UBB/LBB slope bias, skip rangebound", {"max_sl", "bias"}),
              ("3. + no fresh trades after %s" % P["no_new_after"], {"max_sl", "bias", "no_late"}),
              ("4. + one trade per rally", {"max_sl", "bias", "no_late", "one_rally"}),
              ("5. + max %d trades / stop after %d losses" % (P["max_trades_per_day"], P["max_losses_per_day"]), ALL),
              ("6. + only at PDH/PDL/PDC/pivot/wick level", ALL | {"level"})]
    rows, runs = [], {}
    for lab, on in ladder:
        tr = trades(sig, a, P, on)
        runs[lab] = tr
        rows.append({"Step": lab, **_stats(tr, P)})
        r = rows[-1]
        print(f"  {lab}: {r.get('5 yrs trades', 0)} trades | +15 {r.get('5 yrs +15 pts', 0):+} | +20 {r.get('5 yrs +20 pts', 0):+} "
              f"| trail {r.get('5 yrs trail pts', 0):+}")
    lad = pd.DataFrame(rows)
    main_tr = runs[ladder[5][0]]
    OUT.mkdir(parents=True, exist_ok=True)
    j = journal(main_tr)
    j.to_csv(OUT / "pa1_journal.csv", index=False)
    g = main_tr.assign(Year=pd.to_datetime(main_tr["entry_time"]).dt.year)
    yb = g.groupby("Year").agg(Trades=("trail_pts", "size"), **{"+15 pts": ("t1_pts", "sum"), "+20 pts": ("t2_pts", "sum"),
                                "Trail pts": ("trail_pts", "sum")},
                               **{"+15 win %": ("t1_pts", lambda v: round(100 * (v > 0).mean(), 1)),
                                  "+20 win %": ("t2_pts", lambda v: round(100 * (v > 0).mean(), 1)),
                                  "Trail win %": ("trail_pts", lambda v: round(100 * (v > 0).mean(), 1))}).round(0).reset_index()
    yb.to_csv(OUT / "pa1_summary_by_year.csv", index=False)
    _xlsx(lad, yb, j, P)
    print("\n" + lad.to_string(index=False))
    print("\nStep 5 by year (after %.1f pts cost per trade):\n" % P["cost_points_per_trade"] + yb.to_string(index=False))
    print(f"\nsaved {OUT}/pa1_report.xlsx, pa1_journal.csv, pa1_summary_by_year.csv")
    return 0


def _xlsx(lad, yb, j, P):
    from openpyxl.formatting.rule import CellIsRule
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    navy, head, gold = PatternFill("solid", fgColor="0B1F3A"), PatternFill("solid", fgColor="16335C"), "D4AF37"
    titles = {"Rule ladder": "Price Action Strategy 1 - rules added one at a time (pts after %.1f cost / trade)" % P["cost_points_per_trade"],
              "By year": "Strategy 1 (step 5, all Sushen rules) by year - three exit columns",
              "Trades": "Every Strategy 1 trade - your homework format with +15 / +20 / trail exits"}
    with pd.ExcelWriter(OUT / "pa1_report.xlsx", engine="openpyxl") as xw:
        for name, df in (("Rule ladder", lad), ("By year", yb), ("Trades", j)):
            df.to_excel(xw, sheet_name=name, index=False, startrow=3)
            ws = xw.sheets[name]
            ws["A1"], ws["A2"] = titles[name], "Tune on 2021-24; trust only what also holds in 2025-26. Information only, not financial advice."
            for row in ws.iter_rows(min_row=1, max_row=ws.max_row, max_col=ws.max_column):
                for c in row:
                    c.fill = navy
                    if c.row > 4:
                        c.font = Font(color="FFFFFF")
            ws["A1"].font = Font(color=gold, bold=True, size=14)
            ws["A2"].font = Font(color="C9D3E0", italic=True)
            for c in ws[4]:
                c.fill, c.font = head, Font(color=gold, bold=True)
                c.alignment = Alignment(wrap_text=True, horizontal="center")
                c.border = Border(bottom=Side(style="medium", color=gold))
            ws.row_dimensions[4].height = 45
            ws.freeze_panes = "B5"
            for col in ws.iter_cols(min_row=4, max_row=4):
                h, L = str(col[0].value or ""), col[0].column_letter
                ws.column_dimensions[L].width = 46 if h in ("Step", "Entry reason") else 30 if "exit" in h else 12
                if ("pts" in h or h.startswith("Day")) and "Risk" not in h and "Max" not in h:
                    rg = f"{L}5:{L}{ws.max_row}"
                    ws.conditional_formatting.add(rg, CellIsRule(operator="greaterThan", formula=["0"], font=Font(color="3DDC84", bold=True)))
                    ws.conditional_formatting.add(rg, CellIsRule(operator="lessThan", formula=["0"], font=Font(color="FF5C5C", bold=True)))


if __name__ == "__main__":
    raise SystemExit(main())
