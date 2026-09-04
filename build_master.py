"""
NIFTY multi-year master level sheet.

    python build_master.py

Reads EVERY CSV in data/ohlc/ (2019.csv … OHLC.csv), stitches them into one
continuous series, and computes for every session:

    R1-R4 / S1-S4     prior swing extremes, walked backwards
    Gap zones         unfilled gap closes, above and below
    Gann R1-R4/S1-S4  Square-of-9 at 45/90/135/180 degrees
    Time cycles       day counts from the major high and major low

Outputs two files:

    NIFTY_Master_Levels.xlsx   formatted, colour-coded, for reading
    master_levels.csv          machine-readable, for the dashboard tab

COLOUR CODING follows your spec: a SUPPORT that gets broken the next day is
filled GREEN on the day it was projected; a RESISTANCE that gets broken is
filled RED. Worth knowing this is inverted from your original script, which
coloured a resistance hit green and a support hit red — if the original was
what you meant, flip BREAK_SUPPORT_FILL and BREAK_RESISTANCE_FILL below.

TIME CYCLES, IMPORTANT DIFFERENCE FROM YOUR SCRIPT: your version anchors on
the single highest high and lowest low of the ENTIRE file. Over seven years
that means almost every row is measured from one 2020 low and one 2025 high,
so the cycle alerts fire a handful of times in seven years and mean nothing
locally. This version also computes cycles from ROLLING 252-day (one year)
extremes, which is what actually moves markets. Both columns are written so
you can compare.
"""
from __future__ import annotations

import sys
from pathlib import Path

import logging

import numpy as np
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

import config

log = logging.getLogger(__name__)

GANN_DEGREES = (45, 90, 135, 180)
GANN_TIME_CYCLES = (30, 45, 60, 90, 120, 135, 144, 180, 225, 270, 315, 360)
ROLLING_WINDOW = 252          # ~1 trading year, for local cycle anchors
GAP_MIN = 5.0                 # points; smaller than this is not a gap

# your spec: support broken -> green, resistance broken -> red
BREAK_SUPPORT_FILL = PatternFill("solid", start_color="C6EFCE")
BREAK_SUPPORT_FONT = Font(name="Arial", color="006100", bold=True)
BREAK_RESISTANCE_FILL = PatternFill("solid", start_color="FFC7CE")
BREAK_RESISTANCE_FONT = Font(name="Arial", color="9C0006", bold=True)
CYCLE_FILL = PatternFill("solid", start_color="FFEB9C")
CYCLE_FONT = Font(name="Arial", color="9C5700", bold=True)
HEADER_FILL = PatternFill("solid", start_color="1F497D")
HEADER_FONT = Font(name="Arial", color="FFFFFF", bold=True)


def gann_sq9(price: float, degrees: float) -> float:
    """(sqrt(P) + deg/180)^2 — the standard retail Square-of-9 step."""
    if pd.isna(price) or price <= 0:
        return np.nan
    return round((np.sqrt(price) + degrees / 180.0) ** 2, 2)


# ═══════════════════════════════════════════════════════════════════════════
# Load and stitch
# ═══════════════════════════════════════════════════════════════════════════

def _parse_dates(raw: pd.Series, where: str = "") -> pd.Series:
    """
    Parse NSE date strings without silently losing rows.

    THE TRAP: "May" is the only month whose abbreviation equals its full name.
    Given a column starting "01-May-2026", pandas infers %d-%B-%Y, then fails
    on "01-Jun-2026" because that is not "June". With errors="coerce" every
    later row becomes NaT and dropna throws them away — no error, no warning.
    Measured on a real file: 58 of 79 rows disappeared and the levels quietly
    stayed three months stale.

    So: try each known format explicitly, keep whichever parses the most, and
    SAY SO if rows are still lost.
    """
    best, best_n = None, -1
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        got = pd.to_datetime(raw, format=fmt, errors="coerce")
        n = int(got.notna().sum())
        if n > best_n:
            best, best_n = got, n
        if n == len(raw):
            break

    if best_n < len(raw):
        loose = pd.to_datetime(raw, dayfirst=True, errors="coerce", format="mixed")
        if int(loose.notna().sum()) > best_n:
            best, best_n = loose, int(loose.notna().sum())

    if best_n < len(raw):
        lost = len(raw) - best_n
        sample = raw[best.isna()].head(3).tolist()
        log.warning("%s: %d of %d dates unparseable and will be DROPPED "
                    "(e.g. %s)", where or "date column", lost, len(raw), sample)
    return best


def load_all(folder: Path, quiet: bool = False) -> pd.DataFrame:
    """
    Every CSV in the folder, merged and de-duplicated by date.

    `quiet` exists because this is called several times per run by scripts
    that only want the newest date, and each call reprinted the full file
    inventory. In the last end-of-day run that inventory appeared four times
    and pushed the one line that mattered — "0 session(s) added" — into the
    middle of the noise, where it went unnoticed.
    """
    files = sorted(p for p in folder.glob("*.csv")
                   if not p.name.startswith("_"))
    if not files:
        raise FileNotFoundError(f"No CSVs in {folder}")

    frames, report = [], []
    for f in files:
        d = pd.read_csv(f)
        d.columns = [c.strip() for c in d.columns]
        ren = {}
        for want in ("Date", "Open", "High", "Low", "Close"):
            for have in d.columns:
                if have.lower().replace(" ", "") == want.lower():
                    ren[have] = want
        d = d.rename(columns=ren)
        missing = {"Date", "Open", "High", "Low", "Close"} - set(d.columns)
        if missing:
            report.append(f"  {f.name:<16} SKIPPED — missing {sorted(missing)}")
            continue

        d["Date"] = _parse_dates(d["Date"], f.name)
        for c in ("Open", "High", "Low", "Close"):
            d[c] = pd.to_numeric(
                d[c].astype(str).str.replace(",", "", regex=False).str.strip(),
                errors="coerce")
        d = d.dropna(subset=["Date", "Close"])
        report.append(f"  {f.name:<16} {len(d):>5} rows  "
                      f"{d.Date.min():%d-%b-%Y} to {d.Date.max():%d-%b-%Y}")
        frames.append(d[["Date", "Open", "High", "Low", "Close"]])

    if not quiet:
        print("\n".join(report))
    out = (pd.concat(frames, ignore_index=True)
             .sort_values("Date")
             .drop_duplicates(subset=["Date"], keep="last")
             .reset_index(drop=True))
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Level computation
# ═══════════════════════════════════════════════════════════════════════════

def build_rows(df: pd.DataFrame) -> pd.DataFrame:
    n = len(df)
    highs = df["High"].values
    lows = df["Low"].values
    closes = df["Close"].values
    opens = df["Open"].values
    dates = df["Date"].values

    # global anchors (your original approach)
    g_hi_i, g_lo_i = int(np.argmax(highs)), int(np.argmin(lows))
    g_hi_date, g_lo_date = df.Date.iloc[g_hi_i], df.Date.iloc[g_lo_i]

    # rolling local anchors — the ones that actually matter for cycles
    roll_hi_idx = pd.Series(highs).rolling(ROLLING_WINDOW, min_periods=20).apply(
        lambda w: w.idxmax(), raw=False)
    roll_lo_idx = pd.Series(lows).rolling(ROLLING_WINDOW, min_periods=20).apply(
        lambda w: w.idxmin(), raw=False)

    open_gaps: list[dict] = []
    rows = []

    def dstr(j):
        return df.Date.iloc[j].strftime("%d-%m-%Y")

    for i in range(n):
        O, H, L, C = opens[i], highs[i], lows[i], closes[i]
        gap = round(O - closes[i - 1], 2) if i else np.nan

        # ── R1-R4: today's high, then progressively higher prior highs ──────
        # The source DATE travels with each level. A level is "14-08-2026 H
        # R1", not just a number — the date is what goes on the chart label
        # and what lets you check afterwards whether it held.
        R = [np.nan] * 4
        RD = [""] * 4
        seq = [(H, i)]
        for j in range(i - 1, -1, -1):
            if highs[j] > seq[-1][0]:
                seq.append((highs[j], j))
                if len(seq) == 4:
                    break
        for k, (v, j) in enumerate(seq):
            R[k] = round(v, 2)
            RD[k] = dstr(j)

        # ── S1-S4 mirrored ──────────────────────────────────────────────────
        S = [np.nan] * 4
        SD = [""] * 4
        seq = [(L, i)]
        for j in range(i - 1, -1, -1):
            if lows[j] < seq[-1][0]:
                seq.append((lows[j], j))
                if len(seq) == 4:
                    break
        for k, (v, j) in enumerate(seq):
            S[k] = round(v, 2)
            SD[k] = dstr(j)

        # ── gaps: expire the filled, create the new ─────────────────────────
        open_gaps = [g for g in open_gaps
                     if not ((g["type"] == "UP" and L <= g["price"])
                             or (g["type"] == "DOWN" and H >= g["price"]))]
        if i:
            pc = closes[i - 1]
            if O - pc > GAP_MIN:
                open_gaps.append({"type": "UP", "price": pc, "date": dstr(i)})
            elif pc - O > GAP_MIN:
                open_gaps.append({"type": "DOWN", "price": pc, "date": dstr(i)})

        up = sorted((g for g in open_gaps if g["price"] > C),
                    key=lambda g: g["price"])[:3]
        dn = sorted((g for g in open_gaps if g["price"] < C),
                    key=lambda g: -g["price"])[:3]
        U = [round(g["price"], 2) for g in up] + [np.nan] * (3 - len(up))
        UD = [g["date"] for g in up] + [""] * (3 - len(up))
        D = [round(g["price"], 2) for g in dn] + [np.nan] * (3 - len(dn))
        DD = [g["date"] for g in dn] + [""] * (3 - len(dn))

        # ── Gann ────────────────────────────────────────────────────────────
        gR = [gann_sq9(C, d) for d in GANN_DEGREES]
        gS = [gann_sq9(C, -d) for d in GANN_DEGREES]

        # ── time cycles: global and rolling ─────────────────────────────────
        cur = df.Date.iloc[i]
        d_hi = (cur - g_hi_date).days
        d_lo = (cur - g_lo_date).days
        alerts = []
        for lbl, dd in (("Top", d_hi), ("Btm", d_lo)):
            if dd > 0:
                for c in GANN_TIME_CYCLES:
                    if abs(dd - c) <= 1:
                        alerts.append(f"{lbl}+{c}D")

        local = []
        rh, rl = roll_hi_idx.iloc[i], roll_lo_idx.iloc[i]
        if not pd.isna(rh):
            dd = (cur - df.Date.iloc[int(rh)]).days
            for c in GANN_TIME_CYCLES:
                if 0 < dd and abs(dd - c) <= 1:
                    local.append(f"LocalTop+{c}D")
        if not pd.isna(rl):
            dd = (cur - df.Date.iloc[int(rl)]).days
            for c in GANN_TIME_CYCLES:
                if 0 < dd and abs(dd - c) <= 1:
                    local.append(f"LocalBtm+{c}D")

        rows.append({
            "Date": cur, "Open": O, "High": H, "Low": L, "Close": C,
            "Gap_Points": gap,
            "R1": R[0], "R1_Date": RD[0], "R2": R[1], "R2_Date": RD[1],
            "R3": R[2], "R3_Date": RD[2], "R4": R[3], "R4_Date": RD[3],
            "S1": S[0], "S1_Date": SD[0], "S2": S[1], "S2_Date": SD[1],
            "S3": S[2], "S3_Date": SD[2], "S4": S[3], "S4_Date": SD[3],
            "Upper_Gap_1": U[0], "Upper_Gap_1_Date": UD[0],
            "Upper_Gap_2": U[1], "Upper_Gap_2_Date": UD[1],
            "Upper_Gap_3": U[2], "Upper_Gap_3_Date": UD[2],
            "Lower_Gap_1": D[0], "Lower_Gap_1_Date": DD[0],
            "Lower_Gap_2": D[1], "Lower_Gap_2_Date": DD[1],
            "Lower_Gap_3": D[2], "Lower_Gap_3_Date": DD[2],
            "Gann_R1_45": gR[0], "Gann_R2_90": gR[1],
            "Gann_R3_135": gR[2], "Gann_R4_180": gR[3],
            "Gann_S1_45": gS[0], "Gann_S2_90": gS[1],
            "Gann_S3_135": gS[2], "Gann_S4_180": gS[3],
            "Days_from_Major_High": d_hi if d_hi >= 0 else np.nan,
            "Days_from_Major_Low": d_lo if d_lo >= 0 else np.nan,
            "Time_Cycle_Global": " ".join(alerts) or "None",
            "Time_Cycle_Local": " ".join(local) or "None",
        })

    return pd.DataFrame(rows)


# ═══════════════════════════════════════════════════════════════════════════
# Excel
# ═══════════════════════════════════════════════════════════════════════════

RES_COLS = ["R1", "R2", "R3", "R4", "Upper_Gap_1", "Upper_Gap_2", "Upper_Gap_3",
            "Gann_R1_45", "Gann_R2_90", "Gann_R3_135", "Gann_R4_180"]
SUP_COLS = ["S1", "S2", "S3", "S4", "Lower_Gap_1", "Lower_Gap_2", "Lower_Gap_3",
            "Gann_S1_45", "Gann_S2_90", "Gann_S3_135", "Gann_S4_180"]


def write_excel(m: pd.DataFrame, path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "NIFTY Master Levels"

    headers = list(m.columns)
    ws.append(headers)
    idx = {h: i + 1 for i, h in enumerate(headers)}

    arial = Font(name="Arial")
    centre = Alignment(horizontal="center", vertical="center")
    thin = Border(*[Side(style="thin", color="D9D9D9")] * 4)

    hi = m["High"].values
    lo = m["Low"].values

    for i in range(len(m)):
        r = m.iloc[i]
        ws.append([r["Date"].strftime("%d-%b-%Y") if h == "Date"
                   else (None if pd.isna(r[h]) else r[h]) for h in headers])
        row = ws.max_row

        # colour a level on the day it was PROJECTED, based on the NEXT day
        if i < len(m) - 1:
            nh, nl = hi[i + 1], lo[i + 1]
            for h in SUP_COLS:                     # support broken -> GREEN
                v = r[h]
                if pd.notna(v) and nl <= v:
                    c = ws.cell(row=row, column=idx[h])
                    c.fill, c.font = BREAK_SUPPORT_FILL, BREAK_SUPPORT_FONT
            for h in RES_COLS:                     # resistance broken -> RED
                v = r[h]
                if pd.notna(v) and nh >= v:
                    c = ws.cell(row=row, column=idx[h])
                    c.fill, c.font = BREAK_RESISTANCE_FILL, BREAK_RESISTANCE_FONT

        for h in ("Time_Cycle_Global", "Time_Cycle_Local"):
            if r[h] != "None":
                c = ws.cell(row=row, column=idx[h])
                c.fill, c.font = CYCLE_FILL, CYCLE_FONT

    for col in range(1, len(headers) + 1):
        letter = get_column_letter(col)
        # date columns need less room than prices
        ws.column_dimensions[letter].width = 12 if headers[col-1].endswith("_Date") else 15
        hc = ws.cell(row=1, column=col)
        hc.fill, hc.font, hc.alignment = HEADER_FILL, HEADER_FONT, centre

    for row_cells in ws.iter_rows(min_row=2):
        for c in row_cells:
            c.alignment = centre
            c.border = thin
            if c.font is None or c.font.name != "Arial":
                if not c.fill or c.fill.start_color.rgb in (None, "00000000"):
                    c.font = arial

    ws.column_dimensions[get_column_letter(idx["Time_Cycle_Global"])].width = 22
    ws.column_dimensions[get_column_letter(idx["Time_Cycle_Local"])].width = 22
    ws.freeze_panes = "B2"

    # legend, so the colours are self-explaining
    leg = wb.create_sheet("Legend")
    leg["A1"] = "NIFTY Master Levels — legend"
    leg["A1"].font = Font(name="Arial", bold=True, size=13)
    notes = [
        ("", ""),
        ("GREEN fill", "A SUPPORT level that was broken the following day "
                       "(next day's low went below it)"),
        ("RED fill", "A RESISTANCE level that was broken the following day "
                     "(next day's high went above it)"),
        ("AMBER fill", "A Gann time-cycle day"),
        ("", ""),
        ("R1-R4", "Today's high, then progressively higher prior highs walking back"),
        ("S1-S4", "Today's low, then progressively lower prior lows walking back"),
        ("Upper/Lower Gap", "Unfilled gap closes above / below today's close"),
        ("Gann R/S", "(sqrt(Close) + deg/180)^2 at 45/90/135/180 degrees. "
                     "Note these sit at a near-constant PERCENTAGE from price "
                     "(~0.32/0.65/0.97/1.29%), so they are a percentage grid "
                     "rather than discovered structure."),
        ("*_Date columns", "The session that FORMED each level. R1 is today's "
                           "high, R2 the next higher high walking backwards — "
                           "which can be weeks or months old, and that is "
                           "correct, not a gap in the data. The date is what "
                           "goes on the chart label."),
        ("Time_Cycle_Global", "Days from the single highest high / lowest low "
                              "in the whole file"),
        ("Time_Cycle_Local", "Days from the rolling 252-day high / low — fires "
                             "far more often and is anchored to recent structure"),
    ]
    for i, (a, b) in enumerate(notes, start=2):
        leg[f"A{i}"] = a
        leg[f"B{i}"] = b
        leg[f"A{i}"].font = Font(name="Arial", bold=True)
        leg[f"B{i}"].font = Font(name="Arial")
        leg[f"B{i}"].alignment = Alignment(wrap_text=True, vertical="top")
    leg.column_dimensions["A"].width = 22
    leg.column_dimensions["B"].width = 95

    wb.save(path)


def main() -> int:
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else config.OHLC_DIR
    print("=" * 70)
    print("NIFTY MASTER LEVEL SHEET")
    print("=" * 70)
    print(f"\nreading {folder}\n")

    try:
        df = load_all(folder)
    except Exception as exc:                                # noqa: BLE001
        print(f"\nFailed: {exc}")
        return 1

    print(f"\ncombined: {len(df):,} sessions, "
          f"{df.Date.min():%d-%b-%Y} to {df.Date.max():%d-%b-%Y}")
    gaps = df.Date.diff().dt.days
    big = int((gaps > 10).sum())
    if big:
        print(f"  note: {big} break(s) longer than 10 days inside the series")

    print("\ncomputing levels…")
    m = build_rows(df)

    csv_path = folder.parent / "master_levels.csv"
    out = m.copy()
    out["Date"] = out["Date"].dt.strftime("%Y-%m-%d")
    out.to_csv(csv_path, index=False)
    print(f"  csv   -> {csv_path}")

    xlsx_path = folder.parent / "NIFTY_Master_Levels.xlsx"
    write_excel(m, xlsx_path)
    print(f"  excel -> {xlsx_path}")

    last = m.iloc[-1]
    print(f"\nlatest session {last['Date']:%d-%b-%Y}  close {last['Close']:,.2f}")

    # Say so when the newest row is old. This script only READS what is on
    # disk, so running it after a failed fetch happily rebuilds week-old
    # levels and prints them with the same confidence as fresh ones. The
    # levels are not wrong; they are answers to a question about a close
    # that stopped being the latest one several sessions ago, and nothing in
    # the output said so.
    from datetime import date as _date, timedelta as _td
    _behind = (_date.today() - last["Date"].date()).days
    if _behind > 1:
        _sessions = sum(1 for i in range(1, _behind + 1)
                        if (_date.today() - _td(days=i)).weekday() < 5)
        print(f"\n  *** STALE: this close is {_behind} days and about "
              f"{_sessions} trading sessions old. ***")
        print("  Every level above is correct for that date and wrong for")
        print("  today. build_master only reads data/ohlc — it does not")
        print("  fetch. Run:  python fix_levels.py")
    print(f"  R  {last['R1']:,.0f} / {last['R2']:,.0f} / {last['R3']:,.0f} / {last['R4']:,.0f}")
    print(f"  S  {last['S1']:,.0f} / {last['S2']:,.0f} / {last['S3']:,.0f} / {last['S4']:,.0f}")
    print(f"  Gann R  {last['Gann_R1_45']:,.0f} / {last['Gann_R2_90']:,.0f}")
    print(f"  Gann S  {last['Gann_S1_45']:,.0f} / {last['Gann_S2_90']:,.0f}")
    print(f"  cycles  global {last['Time_Cycle_Global']} | local {last['Time_Cycle_Local']}")

    ng = int((m.Time_Cycle_Global != "None").sum())
    nl = int((m.Time_Cycle_Local != "None").sum())
    print(f"\ntime-cycle days over the whole series: global {ng}, local {nl}")
    print("\nThe dashboard's 'Master levels' tab reads master_levels.csv.")
    return 0


if __name__ == "__main__":
    sys.exit(main())


# ═══════════════════════════════════════════════════════════════════════════
# Dated copy-paste levels — for marking up your chart each morning
# ═══════════════════════════════════════════════════════════════════════════

def dated_levels(df: pd.DataFrame, as_of: int | None = None,
                 n_levels: int = 4, n_gaps: int = 3) -> dict:
    """
    Levels with the DATE of the candle that formed each one.

    The date is the point. "24,405.20" is a number; "14-08-2026 H R1 -
    24405.20" tells you which session's high made that level, which is what
    goes on the chart label and what lets you check later whether it held.

    R1 is the most recent session's high, then progressively HIGHER highs
    walking backwards — so R2 is above R1, R3 above R2. Supports mirror it.
    Gaps are unfilled candle closes, dated to the session that created them.
    """
    i = len(df) - 1 if as_of is None else as_of
    highs, lows = df["High"].values, df["Low"].values
    closes, opens = df["Close"].values, df["Open"].values
    dates = df["Date"]

    def dstr(j):
        return dates.iloc[j].strftime("%d-%m-%Y")

    # ── resistances: today's high, then higher highs going back ─────────────
    res = [(dstr(i), float(highs[i]))]
    for j in range(i - 1, -1, -1):
        if highs[j] > res[-1][1]:
            res.append((dstr(j), float(highs[j])))
            if len(res) == n_levels:
                break

    sup = [(dstr(i), float(lows[i]))]
    for j in range(i - 1, -1, -1):
        if lows[j] < sup[-1][1]:
            sup.append((dstr(j), float(lows[j])))
            if len(sup) == n_levels:
                break

    # ── unfilled gaps, walked forward so fills are honoured ─────────────────
    open_gaps: list[dict] = []
    for k in range(1, i + 1):
        o, h, l = opens[k], highs[k], lows[k]
        open_gaps = [g for g in open_gaps
                     if not ((g["type"] == "UP" and l <= g["price"])
                             or (g["type"] == "DOWN" and h >= g["price"]))]
        pc = closes[k - 1]
        if o - pc > GAP_MIN:
            open_gaps.append({"type": "UP", "price": float(pc), "date": dstr(k)})
        elif pc - o > GAP_MIN:
            open_gaps.append({"type": "DOWN", "price": float(pc), "date": dstr(k)})

    c = float(closes[i])
    upper = sorted((g for g in open_gaps if g["price"] > c),
                   key=lambda g: g["price"])[:n_gaps]
    lower = sorted((g for g in open_gaps if g["price"] < c),
                   key=lambda g: -g["price"])[:n_gaps]

    return {
        "date": dates.iloc[i].strftime("%d-%m-%Y"),
        "close": c,
        "resistances": res,
        "supports": sup,
        "upper_gaps": [(g["date"], g["price"]) for g in upper],
        "lower_gaps": [(g["date"], g["price"]) for g in lower],
    }


def format_copy_paste(lv: dict) -> str:
    """Render in the exact layout you use for marking the chart."""
    out = [f"NIFTY levels from the {lv['date']} close ({lv['close']:,.2f})", ""]

    out.append("🔴 Resistances (Copy-Paste)")
    out.append(" ".join(f"{d} H R{i} - {p:.2f}"
                        for i, (d, p) in enumerate(lv["resistances"], 1)))
    out.append("")
    out.append("🟢 Supports (Copy-Paste)")
    out.append(" ".join(f"{d} L S{i} - {p:.2f}"
                        for i, (d, p) in enumerate(lv["supports"], 1)))
    if lv["upper_gaps"]:
        out.append("")
        out.append("🔵 Upper Gaps (Copy-Paste)")
        out.append(" ".join(f"{d} GAP - {p:.2f}" for d, p in lv["upper_gaps"]))
    if lv["lower_gaps"]:
        out.append("")
        out.append("🔵 Lower Gaps (Copy-Paste)")
        out.append(" ".join(f"{d} GAP - {p:.2f}" for d, p in lv["lower_gaps"]))
    return "\n".join(out)
