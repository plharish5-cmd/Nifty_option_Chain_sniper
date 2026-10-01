#!/usr/bin/env python3
"""
participant_workbook_uno.py — append trading days into the trainer's workbook
(Partcipant_Data_Sheet_NEW.xlsm) exactly the way his Excel routine does it.

Runs under the SYSTEM python (/usr/bin/python3) because it needs the LibreOffice
UNO bridge (Ubuntu: sudo apt install libreoffice-calc python3-uno).
It is called by update_participant_sheet.py; you normally never run it directly.

What it replicates, per day (his video, in order):
  Ctrl+Q/W/E/R  -> raw OI of Client/DII/FII/PRO into Retailers/DII/FII/PRO D,F,H,J,L,N,P,R
  Ctrl+L        -> FII stock-option OI into FII!CL,CN,CP,CR
  Ctrl+P/O      -> FII index futures/options (fii_stats) into 'FII Index' B:G and N:S
  Ctrl+I/U      -> FII stock futures/options (fii_stats) into 'FII Stocks' B:G and N:S
  Ctrl+K        -> FII volume into 'FII Vol' C,E,G,I,K,M
  Ctrl+N/B      -> Nifty / BankNifty futures & options (FIIdata rows 39/43) into
                   'Nifty & Bank' B:I and N:U (optional in his video; kept)
  drag down     -> 'Daily Equity' formula rows kept level with the FII sheet (optional; kept)
  NSDL final    -> FII!BD of the trading day before each NSDL reporting date, blanks only
  typed values  -> Nifty close FII!AZ, BankNifty close FII!BB, FII cash equity FII!BE,
                   DII cash equity DII!AB
  double-click fill handle -> every formula column is filled down one row
  holiday rule  -> the date is set on the FII sheet only; the pre-typed future dates
                   below it are re-sequenced to the next weekdays
The three landing sheets (Participant wise Open Interest, Volume, FIIdata) are also
refreshed with the day's raw table, as Data -> Refresh All would.

Everything else (colours, conditional formatting, data bars, charts, VBA macros,
column widths) is left untouched because we edit the trainer's own file.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta

import uno  # noqa: E402  (system python3-uno)
from com.sun.star.beans import PropertyValue
from com.sun.star.sheet.FillDirection import TO_BOTTOM

EPOCH = date(1899, 12, 30)          # spreadsheet day-zero
PORT = 2083

# Raw participant fields in NSE file order (landing sheet columns C..P)
OI_FIELDS = ["fut_idx_long", "fut_idx_short", "fut_stk_long", "fut_stk_short",
             "opt_idx_call_long", "opt_idx_put_long", "opt_idx_call_short", "opt_idx_put_short",
             "opt_stk_call_long", "opt_stk_put_long", "opt_stk_call_short", "opt_stk_put_short",
             "total_long", "total_short"]
# Macro1-4 paste D21:R21 = the first 8 fields spaced one column apart
SPACED_COLS = ["D", "F", "H", "J", "L", "N", "P", "R"]
FII_STOCK_OPT_COLS = ["CL", "CN", "CP", "CR"]          # Ctrl+L
FII_VOL_COLS = ["C", "E", "G", "I", "K", "M"]            # Ctrl+K
FII_VOL_FIELDS = ["fut_idx_long", "fut_idx_short", "opt_idx_call_long",
                  "opt_idx_put_long", "opt_idx_call_short", "opt_idx_put_short"]
STATS_FUT_COLS = ["B", "C", "D", "E", "F", "G"]          # Ctrl+P / Ctrl+I
STATS_OPT_COLS = ["N", "O", "P", "Q", "R", "S"]          # Ctrl+O / Ctrl+U
PARTICIPANT_SHEETS = {"Client": "Retailers", "DII": "DII", "FII": "FII", "Pro": "PRO"}
# Analysis tabs whose formula rows (and charts) he extends by fill-down
# Nifty & Bank: FIIdata!A39:H39 / A43:H43 as his formulas compute them
# (net futures qty, avg buy price, avg sell price, -, net options qty, avg buy strike,
#  avg sell strike, net options Rs Cr); lot sizes are the ones in his FIIdata formulas
NB_BLOCKS = (("NIFTY", 65, "B"), ("BANKNIFTY", 30, "N"))
ANALYSIS_TABS = ["OI vs Spot", "Cumulative", "Vol Vs OI", "Futures",
                 "FII vs spot", "FII PCR", "PRO vs Spot"]


def P(name, value):
    p = PropertyValue()
    p.Name, p.Value = name, value
    return p


def col_idx(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1                                           # 0-based


def serial(d: date) -> float:
    return float((d - EPOCH).days)


def from_serial(v: float) -> date:
    return EPOCH + timedelta(days=int(v))


# ─────────────────────────── LibreOffice session ────────────────────────────
class Office:
    def __init__(self):
        # a private profile + port, so an open LibreOffice window on the desktop is not disturbed
        profile = uno.systemPathToFileUrl("/tmp/lo_participant_profile")
        self.proc = subprocess.Popen(
            ["soffice", f"-env:UserInstallation={profile}", "--headless", "--invisible",
             "--norestore", "--nologo", "--nodefault",
             f"--accept=socket,host=127.0.0.1,port={PORT};urp;"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        local = uno.getComponentContext()
        resolver = local.ServiceManager.createInstanceWithContext(
            "com.sun.star.bridge.UnoUrlResolver", local)
        last = None
        for _ in range(90):
            try:
                self.ctx = resolver.resolve(
                    f"uno:socket,host=127.0.0.1,port={PORT};urp;StarOffice.ComponentContext")
                break
            except Exception as exc:                        # office still starting
                last = exc
                time.sleep(1)
        else:
            self.proc.terminate()
            raise RuntimeError(f"LibreOffice did not start: {last}")
        self.desktop = self.ctx.ServiceManager.createInstanceWithContext(
            "com.sun.star.frame.Desktop", self.ctx)

    def open(self, path: str):
        url = uno.systemPathToFileUrl(os.path.abspath(path))
        # MacroExecutionMode 0 = never run the workbook's macros while we edit it
        return self.desktop.loadComponentFromURL(
            url, "_blank", 0, (P("Hidden", True), P("MacroExecutionMode", 0)))

    def close(self):
        try:
            self.desktop.terminate()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=20)
        except Exception:
            self.proc.kill()


# ─────────────────────────────── sheet helpers ──────────────────────────────
def column_formulas(sheet, col: str, first: int, last: int) -> list[str]:
    rng = sheet.getCellRangeByName(f"{col}{first}:{col}{last}")
    return [row[0] for row in rng.getFormulaArray()]


def last_filled_row(sheet, col: str, start: int = 2, limit: int = 6000) -> int:
    vals = column_formulas(sheet, col, start, limit)
    last = start - 1
    for i, v in enumerate(vals):
        if v not in ("", None):
            last = start + i
    return last


def norm_ref(formula: str) -> str:
    """Excel '=FII!A1697' and LibreOffice '=$FII.A1697' -> 'FII!A1697'."""
    f = (formula or "").strip().lstrip("=+").replace("$", "").replace("'", "")
    if "!" not in f and "." in f:
        f = f.replace(".", "!", 1)
    return f.upper()


def find_row_with_formula(sheet, col: str, formula: str, start: int = 2, limit: int = 6000) -> int | None:
    vals = column_formulas(sheet, col, start, limit)
    target = norm_ref(formula)
    for i, v in enumerate(vals):
        if v and norm_ref(v) == target:
            return start + i
    return None


def set_num(sheet, ref: str, value):
    if value is None:
        return
    sheet.getCellRangeByName(ref).setValue(float(value))


def fill_formulas_down(sheet, row: int, max_col: int = 110) -> int:
    """Copy every formula of row-1 into row where the target cell is empty
    (what double-clicking the fill handle does). Returns the number filled."""
    above = sheet.getCellRangeByPosition(0, row - 2, max_col, row - 2).getFormulaArray()[0]
    here = sheet.getCellRangeByPosition(0, row - 1, max_col, row - 1).getFormulaArray()[0]
    n = 0
    for c, (f_above, f_here) in enumerate(zip(above, here)):
        if isinstance(f_above, str) and f_above.startswith("=") and f_here in ("", None):
            sheet.getCellRangeByPosition(c, row - 2, c, row - 1).fillAuto(TO_BOTTOM, 1)
            n += 1
    return n


def ensure_row_linked(sheet, col: str, formula_prefix: str, fii_row: int) -> int:
    """Row on a dependent sheet whose column A reads =FII!A<fii_row> (or the given prefix).
    If the trainer had not pre-filled that far, extend column A by fill-down."""
    want = f"={formula_prefix}{fii_row}"
    r = find_row_with_formula(sheet, col, want)
    if r:
        return r
    last = last_filled_row(sheet, col)
    m = re.search(r"(\d+)$", norm_ref(sheet.getCellRangeByName(f"{col}{last}").getFormula()))
    gap = fii_row - int(m.group(1)) if m else 0
    if 0 < gap <= 300:                                     # one drag, like his fill handle
        sheet.getCellRangeByName(f"{col}{last}:{col}{last + gap}").fillAuto(TO_BOTTOM, 1)
        if norm_ref(sheet.getCellRangeByName(f"{col}{last + gap}").getFormula()) == norm_ref(want):
            return last + gap
    raise RuntimeError(f"{sheet.Name}: could not find or create a row for {want}")


def fill_formulas_to(sheet, row: int, max_col: int, key_col: str) -> int:
    """His drag-down over a gap: every formula column whose formulas run to (about) the
    same row as key_col is dragged from its own last formula down to `row`. Columns he
    stopped long ago (e.g. Daily Equity B, which ends years earlier) are left alone."""
    key_last = last_filled_row(sheet, key_col)
    if key_last >= row:
        return fill_formulas_down(sheet, row, max_col)
    lo = max(2, key_last - 400)
    block = sheet.getCellRangeByPosition(0, lo - 1, max_col, row - 1).getFormulaArray()
    n = 0
    for c in range(max_col + 1):
        last_f = None
        for i in range(len(block) - 1, -1, -1):
            f = block[i][c]
            if isinstance(f, str) and f.startswith("="):
                last_f = lo + i
                break
        if last_f is None or last_f >= row or key_last - last_f > 5:
            continue
        if any(block[i][c] not in ("", None) for i in range(last_f - lo + 1, row - lo + 1)
               if not (isinstance(block[i][c], str) and block[i][c].startswith("="))):
            continue                                        # typed values below: don't overwrite
        sheet.getCellRangeByPosition(c, last_f - 1, c, row - 1).fillAuto(TO_BOTTOM, 1)
        n += 1
    return n


def nifty_bank_values(stats_all: dict) -> dict[str, list]:
    out = {}
    for prefix, lot, _ in NB_BLOCKS:
        fut, opt = stats_all.get(f"{prefix} FUTURES"), stats_all.get(f"{prefix} OPTIONS")
        if not fut or not opt:
            continue

        def px(amt, qty):
            return amt / qty / lot * 1e7 if qty else None
        out[prefix] = [fut[0] - fut[2], px(fut[1], fut[0]), px(fut[3], fut[2]), None,
                       opt[0] - opt[2], px(opt[1], opt[0]), px(opt[3], opt[2]), round(opt[1] - opt[3], 2)]
    return out


def fill_bd(doc, bd_fill: dict, log) -> None:
    """NSDL final FPI equity into FII!BD (blank cells only, recent rows)."""
    if not bd_fill:
        return
    fii = doc.Sheets.getByName("FII")
    last = last_filled_row(fii, "D")
    for r in range(max(2, last - 40), last + 1):
        v = fii.getCellRangeByName(f"A{r}").getValue()
        if not v:
            continue
        key = f"{from_serial(v):%Y-%m-%d}"
        if key in bd_fill and fii.getCellRangeByName(f"BD{r}").getFormula() == "":
            set_num(fii, f"BD{r}", bd_fill[key])
            log(f"{key}: FII!BD{r} = {bd_fill[key]} (NSDL final)")


# ─────────────────────────────── one trading day ────────────────────────────
def append_day(doc, day: dict, log) -> None:
    d = datetime.strptime(day["date"], "%Y-%m-%d").date()
    sheets = doc.Sheets
    fii = sheets.getByName("FII")

    # 1) FII row: next empty row by column D (raw data), date set on the FII sheet only
    last = last_filled_row(fii, "D")
    last_date = from_serial(fii.getCellRangeByName(f"A{last}").getValue())
    if d <= last_date:
        log(f"{d}: already in the workbook (last row {last} = {last_date}) — skipped")
        return
    n = last + 1
    cur = fii.getCellRangeByName(f"A{n}")
    if cur.getFormula() == "" or from_serial(cur.getValue()) != d:
        fii.getCellRangeByName(f"A{n}").setValue(serial(d))
        # re-sequence the trainer's pre-typed future dates (holiday rule)
        nxt, r = d, n + 1
        while True:
            c = fii.getCellRangeByName(f"A{r}")
            f = c.getFormula()
            if f == "" or f.startswith("="):
                break
            nxt += timedelta(days=1)
            while nxt.weekday() >= 5:
                nxt += timedelta(days=1)
            c.setValue(serial(nxt))
            r += 1

    oi, vol, stats = day["oi"], day["vol"], day["fii_stats"]

    # 2) participant sheets (Ctrl+Q / W / E / R)
    for nse_name, sheet_name in PARTICIPANT_SHEETS.items():
        sh = sheets.getByName(sheet_name)
        row = n if sheet_name == "FII" else ensure_row_linked(sh, "A", "FII!A", n)
        vals = oi[nse_name]
        for col, field in zip(SPACED_COLS, OI_FIELDS[:8]):
            set_num(sh, f"{col}{row}", vals[field])
        if sheet_name == "FII":                               # Ctrl+L + typed values
            for col, field in zip(FII_STOCK_OPT_COLS, OI_FIELDS[8:12]):
                set_num(sh, f"{col}{row}", vals[field])
            set_num(sh, f"AZ{row}", day.get("nifty_close"))
            set_num(sh, f"BB{row}", day.get("banknifty_close"))
            set_num(sh, f"BE{row}", day.get("fii_equity_cr"))     # provisional (NSE)
            if day.get("fii_equity_final_prev_cr") is not None:   # NSDL final, previous row
                set_num(sh, f"BD{row - 1}", day["fii_equity_final_prev_cr"])
        if sheet_name == "DII":
            set_num(sh, f"AB{row}", day.get("dii_equity_cr"))
        filled = fill_formulas_down(sh, row)
        log(f"{d}: {sheet_name} row {row} written, {filled} formulas filled")

    # 3) FII Index (Ctrl+P / O) and FII Stocks (Ctrl+I / U)
    fi = sheets.getByName("FII Index")
    fi_row = ensure_row_linked(fi, "A", "FII!A", n)
    for col, v in zip(STATS_FUT_COLS, stats["INDEX FUTURES"]):
        set_num(fi, f"{col}{fi_row}", v)
    for col, v in zip(STATS_OPT_COLS, stats["INDEX OPTIONS"]):
        set_num(fi, f"{col}{fi_row}", v)
    log(f"{d}: FII Index row {fi_row}, {fill_formulas_down(fi, fi_row, 30)} formulas filled")

    fs = sheets.getByName("FII Stocks")
    fs_row = ensure_row_linked(fs, "A", "'FII Index'!A", fi_row)
    for col, v in zip(STATS_FUT_COLS, stats["STOCK FUTURES"]):
        set_num(fs, f"{col}{fs_row}", v)
    for col, v in zip(STATS_OPT_COLS, stats["STOCK OPTIONS"]):
        set_num(fs, f"{col}{fs_row}", v)
    log(f"{d}: FII Stocks row {fs_row}, {fill_formulas_down(fs, fs_row, 30)} formulas filled")

    # 3b) Nifty & Bank (Ctrl+N / Ctrl+B) — optional in his video, kept
    try:
        nb = sheets.getByName("Nifty & Bank")
        nb_vals = nifty_bank_values(day.get("fii_stats_all", {}))
        if nb_vals:
            nb_row = ensure_row_linked(nb, "A", "'FII Index'!A", fi_row)
            for prefix, _, first in NB_BLOCKS:
                if prefix not in nb_vals:
                    continue
                c0 = col_idx(first)
                for k, v in enumerate(nb_vals[prefix]):
                    if v is not None:
                        nb.getCellByPosition(c0 + k, nb_row - 1).setValue(float(v))
            log(f"{d}: Nifty & Bank row {nb_row}, {fill_formulas_to(nb, nb_row, 25, 'K')} formula columns filled")
    except Exception as exc:
        log(f"{d}: Nifty & Bank skipped: {exc}")

    # 4) FII Vol (Ctrl+K)
    fv = sheets.getByName("FII Vol")
    fv_row = ensure_row_linked(fv, "A", "FII!A", n)
    for col, field in zip(FII_VOL_COLS, FII_VOL_FIELDS):
        set_num(fv, f"{col}{fv_row}", vol["FII"][field])
    log(f"{d}: FII Vol row {fv_row}, {fill_formulas_down(fv, fv_row, 45)} formulas filled")

    # 5) Daily Equity — "just drag everything down" (optional in his video, kept)
    try:
        de = sheets.getByName("Daily Equity")
        de_row = ensure_row_linked(de, "A", "+FII!A", n)
        log(f"{d}: Daily Equity row {de_row}, {fill_formulas_to(de, de_row, 28, 'F')} formula columns filled")
    except Exception as exc:
        log(f"{d}: Daily Equity skipped: {exc}")



def refresh_landing(doc, day: dict) -> None:
    """Put the day's raw tables on the three landing sheets, as Refresh All would."""
    d = datetime.strptime(day["date"], "%Y-%m-%d").date()
    lp = doc.Sheets.getByName("Participant wise Open Interest")
    lp.getCellRangeByName("C7").setString(
        f"Participant wise Open Interest (no. of contracts) in Equity Derivatives as on {d:%b %d, %Y}")
    # table layout on both sheets: participant name in column C, 14 values in D..Q
    for sheet_name, key, first in (("Participant wise Open Interest", "oi", 9), ("Volume", "vol", 7)):
        sh = doc.Sheets.getByName(sheet_name)
        for r, name in zip(range(first, first + 5), ["Client", "DII", "FII", "Pro", "TOTAL"]):
            if name not in day[key]:
                continue
            sh.getCellByPosition(2, r - 1).setString(name)
            for i, field in enumerate(OI_FIELDS):
                sh.getCellByPosition(3 + i, r - 1).setValue(float(day[key][name][field]))
    fd = doc.Sheets.getByName("FIIdata")                     # label in A, 6 values in B..G
    for r in range(2, 25):
        lab = fd.getCellByPosition(0, r - 1).getString().strip().upper()
        if lab in day["fii_stats_all"]:
            for j, v in enumerate(day["fii_stats_all"][lab]):
                fd.getCellByPosition(1 + j, r - 1).setValue(float(v))


def extend_analysis_tabs(doc, log) -> None:
    """Extend the chart-source tabs he keeps current (last row within 3 FII rows) and stretch
    any chart whose range ended on the tab's old last row. Tabs he stopped updating
    (Vol Vs OI, Futures, FII vs spot, FII PCR, PRO vs Spot) are left exactly as they are."""
    fii = doc.Sheets.getByName("FII")
    fii_last = last_filled_row(fii, "D")
    for name in ANALYSIS_TABS:
        try:
            sh = doc.Sheets.getByName(name)
        except Exception:
            continue
        last = last_filled_row(sh, "A")
        f = sh.getCellRangeByName(f"A{last}").getFormula()
        m = re.search(r"(?<![A-Za-z ])FII[.!]\$?[A-Z]+\$?(\d+)", f.replace("$FII.", "FII.").replace("'", ""))
        if not m:
            continue
        ref_row, added = int(m.group(1)), 0
        if fii_last - ref_row > 3:
            continue                     # a tab he stopped updating: leave it exactly as he left it
        while ref_row < fii_last and added < 5:
            sh.getCellRangeByPosition(0, last - 1, 6, last).fillAuto(TO_BOTTOM, 1)
            last += 1
            ref_row += 1
            added += 1
        if not added:
            continue
        old_last = last - added
        # chart ranges are stretched after saving (xlsm_restore_vba.extend_chart_ranges);
        # changing them through UNO would reorder his chart series
        print(f"CHART_EXTEND|{name}|{old_last}|{last}", flush=True)
        log(f"analysis tab '{name}': +{added} rows (now to row {last})")


def main() -> int:
    if len(sys.argv) < 4:
        print("usage: participant_workbook_uno.py <in.xlsm> <out.xlsm> <days.json>")
        return 2
    src, dst, days_path = sys.argv[1:4]
    payload = json.loads(open(days_path, encoding="utf-8").read())
    if isinstance(payload, list):                  # older format: just the days
        payload = {"days": payload}
    days = payload.get("days", [])
    bd_fill = payload.get("bd_fill", {})
    log_lines: list[str] = []

    def log(msg):
        print(msg, flush=True)
        log_lines.append(msg)

    office = Office()
    try:
        doc = office.open(src)
        if doc is None:
            raise RuntimeError(f"LibreOffice could not open {src}")
        for day in sorted(days, key=lambda x: x["date"]):
            append_day(doc, day, log)
        if days:
            refresh_landing(doc, sorted(days, key=lambda x: x["date"])[-1])
            extend_analysis_tabs(doc, log)
        fill_bd(doc, bd_fill, log)
        doc.calculateAll()
        doc.storeToURL(uno.systemPathToFileUrl(os.path.abspath(dst)),
                       (P("FilterName", "Calc MS Excel 2007 VBA XML"),))
        doc.close(True)
        log(f"saved {dst}")
    finally:
        office.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
