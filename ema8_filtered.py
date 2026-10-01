"""
ema8_filtered.py — the 8 EMA trades WITH the filters a good trader would add, tested honestly.

  Filters (switch each on/off in ema8_signals_rules.json -> "filters"):
    trend       CE only when price is above the 1-hour trend line, PE only below (1h EMA 20 = 15-min EMA 80)
    separation  4 EMA and 8 EMA apart by >= sep_min_points at the signal (skips the chop crosses)
    max_sl      skip signal candles whose SL is bigger than max_sl_points
    no_lunch    no entries between lunch_from and lunch_to (the 13:00-14:30 chop)
    day_limits  at most max_trades_per_day, stop after max_losses_per_day losses, stop after a win >= stop_after_win_pts
    exit        partial: book half at +partial_pts, SL to entry once +breakeven_pts, rest trails every candle to +max_pts

  Every filter is added ONE AT A TIME (a ladder), and each step is shown for 2021-2024 (where you would tune it) and
  2025-2026 (untouched test). Costs: cost_points_per_trade taken off every trade.

  .venv/bin/python ema8_filtered.py            # writes the files below
  Output in data/ema8_journal/:
      filtered_all.csv              every filtered trade, your homework columns
      filtered_summary_by_year.csv  year table for the filtered trades
      ema8_filtered_report.xlsx     sheets: Filter ladder, By year (filtered vs all), Filtered trades
Information only - not financial advice.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

import ema8_signals as e8

log = logging.getLogger("ema8_filtered")
HERE = Path(__file__).resolve().parent
DIR = HERE / "data" / "ema8_journal"

DEFAULT = {"trend": True, "trend_ema_span": 80, "separation": True, "sep_min_points": 5,
           "max_sl": True, "max_sl_points": 40, "no_lunch": True, "lunch_from": "13:00", "lunch_to": "14:30",
           "day_limits": True, "max_trades_per_day": 2, "max_losses_per_day": 2, "stop_after_win_pts": 20,
           "exit": True, "partial_pts": 20, "breakeven_pts": 15, "max_pts": 120,
           "cost_points_per_trade": 2.5, "train_until": "2024-12-31"}


def frules() -> dict:
    f = dict(DEFAULT)
    f.update({k: v for k, v in e8.rules().get("filters", {}).items() if not k.startswith("_")})
    return f


# ─────────────────────────────── exit: half at +20, BE at +15, trail rest ───
def sim_partial(a: dict, side: str, j: int, entry: float, sl: float, R: dict, F: dict) -> dict:
    s = 1 if side == "CE" else -1
    O, H, L, C, days, tod, n = a["O"], a["H"], a["L"], a["C"], a["days"], a["tod"], a["n"]
    slip = R["risk"].get("stop_slip_points", 1.0)
    end = j
    while end + 1 < n and days[end + 1] == days[j] and tod[end + 1] <= R["exit"]["exit_by"]:
        end += 1
    stop, half_done, pts_half, mfe = sl, False, 0.0, 0.0
    how, px, kx = "15:15 day end", None, end
    for k in range(j, end + 1):
        kx = k
        if (L[k] <= stop) if s > 0 else (H[k] >= stop):
            px = ((min(stop, O[k]) if s > 0 else max(stop, O[k])) if k > j else stop) - s * slip
            how = "stop loss hit" if stop == sl else ("breakeven stop" if abs(stop - entry) < 0.01 else "trailing SL")
            break
        fav = (H[k] - entry) if s > 0 else (entry - L[k])
        mfe = max(mfe, fav)
        if not half_done and mfe >= F["partial_pts"]:
            half_done, pts_half = True, F["partial_pts"]
        if mfe >= F["max_pts"]:
            px, how = entry + s * F["max_pts"], f"+{F['max_pts']} max target"
            break
        if mfe >= F["breakeven_pts"]:
            stop = max(stop, entry) if s > 0 else min(stop, entry)
        if s * (C[k] - entry) >= 0:                                     # trail every closed candle
            stop = max(stop, L[k] - 1) if s > 0 else min(stop, H[k] + 1)
    if px is None:
        px = C[end]
    rest = s * (px - entry)
    pts = 0.5 * pts_half + 0.5 * rest if half_done else rest
    return {"pts": round(pts - F["cost_points_per_trade"], 1), "exit_px": float(px), "exit_t": pd.Timestamp(a["ts"][kx]),
            "exit_how": ("half +%d, rest %s" % (F["partial_pts"], how)) if half_done else how, "mfe": mfe}


def sim_plain(a, side, j, entry, sl, R, F) -> dict:
    """The current beginner plan (TSL every candle, max +120) for the ladder rows before the exit change."""
    o = e8.simulate(a, side, j, entry, sl, R)
    s = 1 if side == "CE" else -1
    return {"pts": round(s * (o["px_cap"] - entry) - F["cost_points_per_trade"], 1), "exit_px": o["px_cap"],
            "exit_t": o["t_cap"], "exit_how": o["exit_cap"], "mfe": o["mfe_cap"]}


# ─────────────────────────────── run one filter set ─────────────────────────
def run(c: pd.DataFrame, s: pd.DataFrame, a: dict, R: dict, F: dict, on: set[str]) -> pd.DataFrame:
    t = s[s["triggered"]].copy()
    if "trend" in on:
        tr = pd.Series(a["C"]).ewm(span=F["trend_ema_span"], adjust=False).mean().values
        up = np.array([a["C"][i] > tr[i] for i in t["idx"]])
        t = t[np.where(t["side"] == "CE", up, ~up)]
    if "separation" in on:
        t = t[t["sep"] >= F["sep_min_points"]]
    if "max_sl" in on:
        t = t[t["risk"] <= F["max_sl_points"]]
    if "no_lunch" in on:
        tm = pd.to_datetime(t["trigger_time"]).dt.strftime("%H:%M")
        t = t[~((tm >= F["lunch_from"]) & (tm < F["lunch_to"]))]
    sim = sim_partial if "exit" in on else sim_plain
    rows = []
    for d, g in t.sort_values("trigger_time").groupby("day"):
        n_tr = n_loss = 0
        busy_until = None
        for _, x in g.iterrows():
            if busy_until is not None and x["trigger_time"] <= busy_until:
                continue                                   # one trade at a time
            if "day_limits" in on and (n_tr >= F["max_trades_per_day"] or n_loss >= F["max_losses_per_day"]):
                break
            j = int(np.searchsorted(a["ts"], np.datetime64(x["trigger_time"])))
            o = sim(a, x["side"], j, x["entry"], x["sl"], R, F)
            busy_until = o["exit_t"]
            n_tr += 1
            n_loss += o["pts"] < 0
            rows.append({**x.to_dict(), **o})
            if "day_limits" in on and o["pts"] >= F["stop_after_win_pts"]:
                break
    return pd.DataFrame(rows)


def stats(tr: pd.DataFrame, F: dict) -> dict:
    if not len(tr):
        return {"Trades": 0}
    tr = tr.copy()
    tr["yr"] = pd.to_datetime(tr["trigger_time"]).dt.year
    cut = pd.Timestamp(F["train_until"]).year
    out = {}
    for lab, g in (("2021-24 (tune)", tr[tr["yr"] <= cut]), ("2025-26 (test)", tr[tr["yr"] > cut]), ("All 5 yrs", tr)):
        days = g.groupby("day")["pts"].sum()
        out[f"{lab} trades"] = len(g)
        out[f"{lab} pts"] = round(g["pts"].sum())
        out[f"{lab} win %"] = round(100 * (g["pts"] > 0).mean(), 1) if len(g) else 0
        out[f"{lab} pts/trade"] = round(g["pts"].mean(), 2) if len(g) else 0
        out[f"{lab} green days %"] = round(100 * (days > 0).mean(), 1) if len(days) else 0
    return out


def journal_rows(tr: pd.DataFrame, F: dict) -> pd.DataFrame:
    rows = []
    for d, g in tr.sort_values("trigger_time").groupby("day"):
        tot = 0.0
        for n_, (_, x) in enumerate(g.iterrows(), 1):
            tot += x["pts"]
            rows.append({"Date": f"{d:%d-%m-%Y}", "Trade #": n_, "Side": x["side"],
                         "Entry time": f"{x['trigger_time']:%H:%M}", "Entry price": round(x["entry"], 1),
                         "Entry reason": e8._reason(x), "SL": round(x["sl"], 1), "Risk pts": round(x["risk"], 1),
                         "Exit time": f"{x['exit_t']:%H:%M}", "Exit price": round(x["exit_px"], 1),
                         "Exit reason": x["exit_how"], "Profit": x["pts"] if x["pts"] > 0 else 0.0,
                         "Loss": x["pts"] if x["pts"] < 0 else 0.0, "Max move pts": round(x["mfe"], 1),
                         "15 pts worked": "YES" if x["mfe"] >= 15 else "no",
                         "20 pts worked": "YES" if x["mfe"] >= 20 else "no", "Day total": None})
        rows[-1]["Day total"] = round(tot, 1)
    return pd.DataFrame(rows)


def by_year(tr: pd.DataFrame) -> pd.DataFrame:
    g = tr.assign(Year=pd.to_datetime(tr["trigger_time"]).dt.year)
    days = g.groupby(["Year", "day"])["pts"].sum().reset_index()
    y = g.groupby("Year").agg(Trades=("pts", "size"), Points=("pts", "sum"),
                              Win_pct=("pts", lambda v: round(100 * (v > 0).mean(), 1)),
                              Pts_per_trade=("pts", lambda v: round(v.mean(), 2)))
    y["Green_days_pct"] = days.groupby("Year")["pts"].apply(lambda v: round(100 * (v > 0).mean(), 1))
    y["Points"] = y["Points"].round(0)
    return y.reset_index()


# ─────────────────────────────── main ───────────────────────────────────────
def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    pd.set_option("display.width", 250, "display.max_columns", 30)
    R, F = e8.rules_for("15m"), frules()
    R["risk"]["max_sl_points"] = max(R["risk"]["max_sl_points"], 90)
    h = e8._history("15m")
    if h is None or not len(h):
        print("no 15-min history")
        return 1
    c = h.reset_index(drop=True)
    print(f"scanning {len(c):,} 15-min candles ...")
    s = e8.scan(c, R)
    a = e8._arrays(c, R)
    ladder = [("0. All 8 EMA trades (your current plan)", set()),
              ("1. + 1-hour trend filter", {"trend"}),
              ("2. + 4/8 EMA separated", {"trend", "separation"}),
              ("3. + SL <= %d pts" % F["max_sl_points"], {"trend", "separation", "max_sl"}),
              ("4. + no entries %s-%s" % (F["lunch_from"], F["lunch_to"]), {"trend", "separation", "max_sl", "no_lunch"}),
              ("5. + max %d trades / 2 losses / stop after +%d" % (F["max_trades_per_day"], F["stop_after_win_pts"]),
               {"trend", "separation", "max_sl", "no_lunch", "day_limits"}),
              ("6. + half at +%d, BE at +%d, trail rest to +%d" % (F["partial_pts"], F["breakeven_pts"], F["max_pts"]),
               {"trend", "separation", "max_sl", "no_lunch", "day_limits", "exit"})]
    rows, runs = [], {}
    for lab, on in ladder:
        on = {k for k in on if F.get(k, True)}              # a filter switched off in the rules is skipped
        tr = run(c, s, a, R, F, on)
        runs[lab] = tr
        rows.append({"Step": lab, **stats(tr, F)})
        print(f"  {lab}: {rows[-1].get('All 5 yrs trades', 0)} trades, {rows[-1].get('All 5 yrs pts', 0):+} pts")
    lad = pd.DataFrame(rows)
    final = runs[ladder[-1][0]]
    base = runs[ladder[0][0]]
    DIR.mkdir(parents=True, exist_ok=True)
    j = journal_rows(final, F)
    j.to_csv(DIR / "filtered_all.csv", index=False)
    yf, yb = by_year(final), by_year(base)
    yf.to_csv(DIR / "filtered_summary_by_year.csv", index=False)
    cmp_ = yb.merge(yf, on="Year", suffixes=(" (all)", " (filtered)"))
    _xlsx(lad, cmp_, j, F)
    print("\n" + lad.to_string(index=False))
    print("\n" + cmp_.to_string(index=False))
    print(f"\nsaved {DIR / 'ema8_filtered_report.xlsx'}, filtered_all.csv, filtered_summary_by_year.csv")
    return 0


def _xlsx(lad: pd.DataFrame, cmp_: pd.DataFrame, j: pd.DataFrame, F: dict) -> None:
    from openpyxl.formatting.rule import CellIsRule
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    out = DIR / "ema8_filtered_report.xlsx"
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        lad.to_excel(xw, sheet_name="Filter ladder", index=False, startrow=3)
        cmp_.to_excel(xw, sheet_name="By year", index=False, startrow=3)
        j.to_excel(xw, sheet_name="Filtered trades", index=False, startrow=3)
        navy, head = PatternFill("solid", fgColor="0B1F3A"), PatternFill("solid", fgColor="16335C")
        gold = "D4AF37"
        titles = {"Filter ladder": "8 EMA filters, added one at a time (after %.1f pts cost per trade)" % F["cost_points_per_trade"],
                  "By year": "By year: all 8 EMA trades vs filtered trades (points after costs)",
                  "Filtered trades": "Filtered trades, your homework columns"}
        for name, ws in xw.sheets.items():
            ws["A1"] = titles[name]
            ws["A1"].font = Font(color=gold, bold=True, size=14)
            ws["A2"] = ("Tune on 2021-24, trust only if 2025-26 (untouched) is also positive. "
                        "Information only, not financial advice.")
            ws["A2"].font = Font(color="C9D3E0", italic=True)
            for row in ws.iter_rows(min_row=1, max_row=ws.max_row, max_col=ws.max_column):
                for x in row:
                    x.fill = navy
                    if x.row > 4:
                        x.font = Font(color="FFFFFF")
            for x in ws[4]:
                x.fill, x.font = head, Font(color=gold, bold=True)
                x.alignment = Alignment(wrap_text=True, horizontal="center")
                x.border = Border(bottom=Side(style="medium", color=gold))
            ws.row_dimensions[4].height = 45
            ws.freeze_panes = "B5"
            for col in ws.iter_cols(min_row=4, max_row=4):
                h = str(col[0].value or "")
                L = col[0].column_letter
                ws.column_dimensions[L].width = 44 if h in ("Step", "Entry reason", "Exit reason") else 13
                if any(k in h for k in ("pts", "Points", "Profit", "Loss", "Day total")) and "Risk" not in h and "Max" not in h:
                    rng = f"{L}5:{L}{ws.max_row}"
                    ws.conditional_formatting.add(rng, CellIsRule(operator="greaterThan", formula=["0"], font=Font(color="3DDC84", bold=True)))
                    ws.conditional_formatting.add(rng, CellIsRule(operator="lessThan", formula=["0"], font=Font(color="FF5C5C", bold=True)))


if __name__ == "__main__":
    raise SystemExit(main())
