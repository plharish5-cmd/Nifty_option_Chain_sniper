"""
update_participant_sheet.py — the trainer's evening routine, automated.

  .venv/bin/python update_participant_sheet.py              # catch up to today
  .venv/bin/python update_participant_sheet.py --date 2026-09-28
  .venv/bin/python update_participant_sheet.py --nifty 23140 --banknifty 55580 \
        --fii-equity -3273.17 --dii-equity 2595.15          # override typed values (latest day)

Reads   data/participant_sheet/Partcipant_Data_Sheet_NEW.xlsm   (his workbook, updated in place)
Writes  the same file (previous version kept in data/participant_sheet/backups/)
        data/NSE_Participant_OI_and_Option_Chain_Master.xlsx     (copy the app's loader reads)
        data/participants.parquet                                (app's history, appended)
        data/participant_sheet/update.log

NSDL final FPI equity (FII!BD) is fetched every run and written into the blank BD cell of
the trading day before each NSDL reporting date (his rule: NSDL is one day late).

Holidays / not yet published: a date whose NSE files return 404 is skipped if it is a
past day (holiday) and stops the run if it is today (NSE publishes 18:30–23:00).
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path

import warnings

import openpyxl

warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")

import config
import nsdl_fpi
import participant_nse as pn
import xlsm_restore_vba

SHEET_DIR = config.DATA_DIR / "participant_sheet"
WORKBOOK = SHEET_DIR / "Partcipant_Data_Sheet_NEW.xlsm"
BACKUPS = SHEET_DIR / "backups"
ORIGINAL = SHEET_DIR / "original" / "Partcipant_Data_Sheet_NEW.xlsm"   # trainer's untouched file
APP_COPY = config.DATA_DIR / "NSE_Participant_OI_and_Option_Chain_Master.xlsx"
UNO_SCRIPT = Path(__file__).resolve().parent / "participant_workbook_uno.py"
SYSTEM_PY = "/usr/bin/python3"                 # has python3-uno; the venv usually does not
KEEP_BACKUPS = 10

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("participant_sheet")


def blank_bd_dates(path: Path, lookback: int = 25) -> tuple[list[date], list[date]]:
    """(all filled FII dates in the lookback window, those whose BD is still blank)."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True, keep_vba=False)
    ws = wb["FII"]
    rows = []
    for r in ws.iter_rows(min_row=2, min_col=1, max_col=56, values_only=True):
        if r[3] not in (None, "") and isinstance(r[0], (datetime, date)):
            d = r[0].date() if isinstance(r[0], datetime) else r[0]
            rows.append((d, r[55]))
    wb.close()
    rows = rows[-lookback:]
    return [d for d, _ in rows], [d for d, bd in rows if bd in (None, "")]


def nsdl_bd_fill(all_dates: list[date], blanks: list[date]) -> dict[str, float]:
    try:
        nsdl = nsdl_fpi.daily_equity()
    except Exception as exc:
        log.warning("NSDL daily FPI fetch failed: %s", exc)
        return {}
    mapped = nsdl_fpi.assign_to_rows(nsdl, all_dates)
    return {f"{d:%Y-%m-%d}": v for d, v in mapped.items() if d in blanks}


def last_date_in_workbook(path: Path) -> date:
    """Last FII row that has raw data in column D (same test the UNO writer uses)."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True, keep_vba=False)
    ws = wb["FII"]
    last = None
    for row in ws.iter_rows(min_row=2, min_col=1, max_col=4, values_only=True):
        if row[3] not in (None, "") and isinstance(row[0], (datetime, date)):
            last = row[0]
    wb.close()
    if last is None:
        raise RuntimeError("no filled FII rows found")
    return last.date() if isinstance(last, datetime) else last


def trading_days(after: date, until: date) -> list[date]:
    out, d = [], after + timedelta(days=1)
    while d <= until:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def rotate_backup(path: Path) -> None:
    BACKUPS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(path, BACKUPS / f"{path.stem}_{stamp}{path.suffix}")
    olds = sorted(BACKUPS.glob(f"{path.stem}_*{path.suffix}"))
    for p in olds[:-KEEP_BACKUPS]:
        p.unlink()


def run(args) -> int:
    SHEET_DIR.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(SHEET_DIR / "update.log")
    fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    log.addHandler(fh)

    if not WORKBOOK.exists():
        log.error("Put the trainer's workbook at %s first.", WORKBOOK)
        return 2
    if not ORIGINAL.exists():                 # first run: keep his pristine file for the macros
        ORIGINAL.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(WORKBOOK, ORIGINAL)
        log.info("Saved the trainer's original workbook to %s (source of his macros).", ORIGINAL)

    last = last_date_in_workbook(WORKBOOK)
    today = date.today()
    wanted = [datetime.strptime(args.date, "%Y-%m-%d").date()] if args.date else trading_days(last, today)
    wanted = [d for d in wanted if d > last]

    overrides = {"nifty_close": args.nifty, "banknifty_close": args.banknifty,
                 "fii_equity_cr": args.fii_equity, "dii_equity_cr": args.dii_equity}
    days: list[dict] = []
    for d in wanted:
        try:
            is_latest = d == wanted[-1]
            day = pn.build_day(d, overrides if is_latest else None, with_cash=(d == today))
            if day["nifty_close"] is None:
                log.warning("%s: Nifty close not found — AZ left blank (pass --nifty).", d)
            days.append(day)
            log.info("%s: NSE files parsed (FII put short %.0f / call short %.0f)", d,
                     day["oi"]["FII"]["opt_idx_put_short"], day["oi"]["FII"]["opt_idx_call_short"])
        except pn.NotPublished:
            if d == today and datetime.now().time() < time(23, 30):
                log.info("%s: NSE files not published yet — will retry later.", d)
                break
            log.info("%s: no NSE files — treated as a holiday.", d)
        except Exception as exc:                          # parse problem: stop, don't write half a day
            log.error("%s: %s — stopping before writing.", d, exc)
            return 1

    # NSDL final FPI equity -> FII!BD of the previous trading day (blank cells only)
    all_dates, blanks = blank_bd_dates(WORKBOOK)
    new_dates = [datetime.strptime(x["date"], "%Y-%m-%d").date() for x in days]
    bd_fill = nsdl_bd_fill(all_dates + new_dates, blanks + new_dates)
    if args.fii_equity_prev is not None:
        prev = sorted(all_dates + new_dates)[-2]
        bd_fill[f"{prev:%Y-%m-%d}"] = args.fii_equity_prev
    if bd_fill:
        log.info("NSDL final equity to fill: %s", ", ".join(f"{k} {v:+,.0f}" for k, v in sorted(bd_fill.items())))

    if not days and not bd_fill:
        log.info("Workbook already up to date (last day %s).", last)
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        days_json = Path(tmp) / "days.json"
        days_json.write_text(json.dumps({"days": days, "bd_fill": bd_fill}))
        out = Path(tmp) / WORKBOOK.name
        cmd = [SYSTEM_PY, str(UNO_SCRIPT), str(WORKBOOK), str(out), str(days_json)]
        log.info("Writing %d day(s), %d BD value(s) with LibreOffice…", len(days), len(bd_fill))
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        for line in (res.stdout or "").splitlines():
            log.info("  %s", line)
        if res.returncode != 0 or not out.exists():
            log.error("LibreOffice step failed:\n%s", res.stderr[-3000:])
            return 1
        info = xlsm_restore_vba.restore(ORIGINAL, out)       # his Ctrl-key macros + code names
        extends = [(p[1], int(p[2]), int(p[3])) for p in
                   (ln.split("|") for ln in (res.stdout or "").splitlines() if ln.startswith("CHART_EXTEND|"))]
        n_ch = xlsm_restore_vba.extend_chart_ranges(out, extends)
        if extends:
            log.info("Chart ranges extended: %d (%s)", n_ch, ", ".join(f"{t} to row {n}" for t, _, n in extends))
        log.info("Macros restored (%d sheet code names, %d bytes of VBA)", info["sheets_named"], info["vba_bytes"])
        rotate_backup(WORKBOOK)
        shutil.copy2(out, WORKBOOK)
    shutil.copy2(WORKBOOK, APP_COPY)

    try:                                                  # keep the app's history in step
        import participant_history as ph
        added, refused = ph.append(str(APP_COPY))
        log.info("participants.parquet: +%s rows (refused %s)", added, refused)
    except Exception as exc:
        log.warning("parquet append skipped: %s", exc)

    try:
        import participant_ratios
        log.info(participant_ratios.summary_line(WORKBOOK))
    except Exception as exc:
        log.warning("ratio summary skipped: %s", exc)
    try:
        import participant_correlation
        log.info(participant_correlation.summary_line(WORKBOOK))
    except Exception as exc:
        log.warning("correlation summary skipped: %s", exc)
    log.info("Done: %s", ", ".join(d["date"] for d in days) or "BD only")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", help="one date YYYY-MM-DD (default: every missing trading day up to today)")
    ap.add_argument("--nifty", type=float, help="Nifty close for FII!AZ (latest day)")
    ap.add_argument("--banknifty", type=float, help="BankNifty close for FII!BB (latest day)")
    ap.add_argument("--fii-equity", type=float, help="FII cash net ₹Cr, provisional (FII!BE)")
    ap.add_argument("--dii-equity", type=float, help="DII cash net ₹Cr (DII!AB)")
    ap.add_argument("--fii-equity-prev", type=float, help="NSDL final FII equity for the PREVIOUS day (FII!BD)")
    return run(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
