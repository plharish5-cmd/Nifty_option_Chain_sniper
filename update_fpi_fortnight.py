"""
update_fpi_fortnight.py — the trainer's fortnightly FPI sector routine, automated.

  .venv/bin/python update_fpi_fortnight.py            # add every published fortnight not yet in Sheet2
  .venv/bin/python update_fpi_fortnight.py --end 2026-09-15

His steps (video 2, 26:05-32:45) and what this does for each:
  NSDL > FPI Investments > Fortnightly Sector-wise data, report ending 15th / month end
  (published 2-4 days after the fortnight)       -> tries it; not published yet = stop quietly
  Data > Queries & Connections > Table 0 > Advanced Editor, change the date after "_"
                                                   -> same edit to his Power Query URL, so
                                                      Refresh All in Excel fetches the same report
  copy Table 0 down to Grand Total, Sheet2 at "Automobile...", Paste Values
                                                   -> new Sheet2 column, values only, his cell style
  type the header by hand ("Feb 1-15")            -> header typed as text ("Sep 1-15"), so Excel
                                                      does not turn it into a date as it did for AY/AZ
Colours: his rule on Sheet2 D4:XFD25 (green > 0, red < 0) covers new columns automatically.

Workbook: data/fpi_fortnight/FPI Fortnight Data.xlsx (original kept in original/, backups/ keeps 10)
History : data/fpi_fortnight/fpi_sectors.csv   (end, label, sector, net_cr) for the app tab
"""
from __future__ import annotations

import argparse
import base64
import calendar
import io
import logging
import re
import shutil
import struct
import sys
import tempfile
import warnings
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import openpyxl
import pandas as pd

warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")

import config
import nsdl_fpi
from xlsm_restore_vba import _sheet_files

DIR = config.DATA_DIR / "fpi_fortnight"
WORKBOOK = DIR / "FPI Fortnight Data.xlsx"
ORIGINAL = DIR / "original" / "FPI Fortnight Data.xlsx"
BACKUPS = DIR / "backups"
HISTORY = DIR / "fpi_sectors.csv"
SHEET = "Sheet2"
HEADER_STYLE_FROM_ROW1 = True
KEEP_BACKUPS = 10

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("fpi_fortnight")

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
MONTHS["sept"] = 9


# ───────────────────────────── reading Sheet2 ───────────────────────────────
def col_letter(n: int) -> str:
    return openpyxl.utils.get_column_letter(n)


def parse_headers(ws) -> list[tuple[int, date]]:
    """[(column number, fortnight end)] for D.. onwards. Handles his header styles:
    'June 16-30, 2024', 'Jan 15-31', 'May 1 -15', and cells Excel turned into dates."""
    c1 = str(ws.cell(1, 3).value or "")
    ym = re.search(r"(\d{4})", c1)
    year = int(ym.group(1)) if ym else 2024
    out: list[tuple[int, date]] = []
    prev: date | None = None
    for c in range(4, ws.max_column + 1):
        v = ws.cell(1, c).value
        if v in (None, ""):
            continue
        explicit = None
        if isinstance(v, datetime):                          # 'Aug 1-15' typed and turned into a date
            month = v.month
            second = bool(prev and prev.month == month and prev.day == 15)
        else:
            s = str(v).strip().lower()
            m = re.search(r"([a-z]+)", s)
            word = m.group(1) if m else ""
            month = MONTHS.get(word) or MONTHS.get(word[:3])
            if not month:
                continue
            days = re.search(r"(\d{1,2})\s*-\s*(\d{1,2})", s)
            second = bool(days and int(days.group(1)) >= 15)
            y = re.search(r"(\d{4})", s)
            explicit = int(y.group(1)) if y else None
        if explicit:
            year = explicit
        elif prev:
            year = prev.year + (1 if month < prev.month else 0)
        end = date(year, month, calendar.monthrange(year, month)[1] if second else 15)
        out.append((c, end))
        prev = end
    return out


def sector_rows(ws) -> dict[str, int]:
    rows = {}
    for r in range(4, ws.max_row + 1):
        name = ws.cell(r, 2).value
        if name:
            rows[_key(str(name))] = r
    return rows


def _key(name: str) -> str:
    s = name.lower().replace("&", " and ")
    return re.sub(r"[^a-z]+", " ", s).strip()


# ─────────────────────────── writing (XML level) ────────────────────────────
def _cell_xml(ref: str, style: str | None, value) -> str:
    s = f' s="{style}"' if style else ""
    if isinstance(value, str):
        t = value.replace("&", "&amp;").replace("<", "&lt;")
        return f'<c r="{ref}"{s} t="inlineStr"><is><t>{t}</t></is></c>'
    return f'<c r="{ref}"{s}><v>{value:g}</v></c>'


def _style_of(row_xml: str, col: str) -> str | None:
    m = re.search(rf'<c r="{col}\d+"([^>]*)', row_xml)
    if m:
        s = re.search(r's="(\d+)"', m.group(1))
        return s.group(1) if s else None
    return None


def add_column(xml: str, new_col: str, prev_col: str, values: dict[int, object],
               header_style_col: str) -> str:
    """Append one cell per row (rows must not already have cells right of prev_col)."""
    def fix_row(m):
        r = int(m.group(1))
        body = m.group(3)
        if r not in values:
            return m.group(0)
        style = _style_of(body, header_style_col if r == 1 else prev_col)
        attrs = re.sub(r'\sspans="[^"]*"', "", m.group(2))
        return f'<row r="{r}"{attrs}>{body}{_cell_xml(f"{new_col}{r}", style, values[r])}</row>'
    xml = re.sub(r'<row r="(\d+)"([^>]*)>(.*?)</row>', fix_row, xml, flags=re.S)
    m = re.search(r'<dimension ref="([A-Z]+)(\d+):([A-Z]+)(\d+)"/>', xml)
    if m:
        last = max(openpyxl.utils.column_index_from_string(m.group(3)),
                   openpyxl.utils.column_index_from_string(new_col))
        xml = xml.replace(m.group(0), f'<dimension ref="{m.group(1)}{m.group(2)}:{col_letter(last)}{m.group(4)}"/>')
    return xml


def set_query_url(item_xml: bytes, new_tag: str) -> bytes:
    """His Advanced-Editor step: change the date after '_' in the Table 0 URL
    inside the workbook's Power Query package (DataMashup)."""
    enc = "utf-16" if item_xml[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
    text = item_xml.decode(enc)
    m = re.search(r"(<DataMashup[^>]*>)([A-Za-z0-9+/=\s]+)(</DataMashup>)", text)
    if not m:
        raise ValueError("DataMashup not found")
    raw = base64.b64decode(m.group(2))
    version, n = struct.unpack("<II", raw[:8])
    pkg, rest = raw[8:8 + n], raw[8 + n:]
    zin = zipfile.ZipFile(io.BytesIO(pkg))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "Formulas/Section1.m":
                m_code = data.decode("utf-8-sig")
                m_code, k = re.subn(r"(shared #\"Table 0\" = let\s+Source = [^\n]*FIIInvestSector_)[A-Za-z]{3}\d{6}(\.html)",
                                    rf"\g<1>{new_tag}\2", m_code)
                if not k:
                    raise ValueError("Table 0 URL not found in the query")
                data = ("﻿" + m_code).encode("utf-8") if data.startswith(b"\xef\xbb\xbf") else m_code.encode("utf-8")
            zout.writestr(item, data)
    new_pkg = buf.getvalue()
    new_raw = struct.pack("<II", version, len(new_pkg)) + new_pkg + rest
    b64 = base64.b64encode(new_raw).decode("ascii")
    return (text[:m.start(2)] + b64 + text[m.end(2):]).encode(enc)


def write_reports(src: Path, dst: Path, reports: list[dict]) -> None:
    wb = openpyxl.load_workbook(src, data_only=False)
    ws = wb[SHEET]
    heads = parse_headers(ws)
    last_col = heads[-1][0]
    rows = sector_rows(ws)
    wb.close()

    with zipfile.ZipFile(src) as z:
        sheet_path = _sheet_files(z)[SHEET]
        xml = z.read(sheet_path).decode("utf-8")
        item_name = next((n for n in z.namelist() if re.match(r"customXml/item\d+\.xml$", n)
                          and (b"DataMashup" in z.read(n) or "DataMashup".encode("utf-16-le") in z.read(n))),
                         None)
        if item_name is None:
            log.warning("no Power Query package found — Sheet2 is still updated")
        item = z.read(item_name) if item_name else None

    for rep in reports:
        new_col = col_letter(last_col + 1)
        values: dict[int, object] = {1: rep["label"], 2: "IN INR Cr.", 3: "Equity"}
        missing = []
        for name, v in rep["sectors"].items():
            r = rows.get(_key(name))
            if r:
                values[r] = v
            else:
                missing.append(name)
        gt = rows.get(_key("Grand Total"))
        if gt:
            values[gt] = rep["grand_total"]
        if missing:
            log.warning("%s: sectors not in Sheet2, left out: %s", rep["label"], ", ".join(missing))
        xml = add_column(xml, new_col, col_letter(last_col), values,
                         header_style_col=col_letter(max(4, last_col - 2)))
        log.info("%s -> Sheet2 column %s (Grand Total %s)", rep["label"], new_col, f"{rep['grand_total']:,.0f}")
        last_col += 1

    if item is not None:
        try:
            item = set_query_url(item, nsdl_fpi.fortnight_tag(reports[-1]["end"]))
            log.info("Power Query 'Table 0' URL now ends _%s.html", nsdl_fpi.fortnight_tag(reports[-1]["end"]))
        except Exception as exc:
            log.warning("Power Query URL not changed (%s) — change it in Excel's Advanced Editor if you use Refresh.", exc)
            item = None

    tmp = Path(tempfile.mkstemp(suffix=".xlsx")[1])
    with zipfile.ZipFile(src) as zs, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zt:
        for it in zs.infolist():
            data = zs.read(it.filename)
            if it.filename == sheet_path:
                data = xml.encode("utf-8")
            elif item is not None and it.filename == item_name:
                data = item
            zt.writestr(it, data)
    shutil.move(str(tmp), dst)


# ─────────────────────────────── history / app ──────────────────────────────
def sheet_history(path: Path = WORKBOOK) -> pd.DataFrame:
    """Sheet2 as a long table: end, label, sector, net_cr (Grand Total included)."""
    wb = openpyxl.load_workbook(path, data_only=True, read_only=False)
    ws = wb[SHEET]
    heads = parse_headers(ws)
    recs = []
    for r in range(4, ws.max_row + 1):
        name = ws.cell(r, 2).value
        if not name:
            continue
        for c, end in heads:
            v = ws.cell(r, c).value
            if isinstance(v, (int, float)):
                recs.append({"end": end, "label": nsdl_fpi.fortnight_label(end), "sector": str(name).strip(),
                             "net_cr": float(v)})
    wb.close()
    return pd.DataFrame(recs)


def rotate_backup(path: Path) -> None:
    BACKUPS.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, BACKUPS / f"{path.stem}_{datetime.now():%Y%m%d_%H%M%S}{path.suffix}")
    for p in sorted(BACKUPS.glob(f"{path.stem}_*{path.suffix}"))[:-KEEP_BACKUPS]:
        p.unlink()


def run(args) -> int:
    DIR.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(DIR / "update.log")
    fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    log.addHandler(fh)
    if not WORKBOOK.exists():
        log.error("Put the trainer's 'FPI Fortnight Data.xlsx' at %s first.", WORKBOOK)
        return 2
    if not ORIGINAL.exists():
        ORIGINAL.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(WORKBOOK, ORIGINAL)

    wb = openpyxl.load_workbook(WORKBOOK, read_only=False)
    heads = parse_headers(wb[SHEET])
    wb.close()
    last_end = heads[-1][1]
    log.info("Sheet2 last fortnight: %s (column %s)", nsdl_fpi.fortnight_label(last_end), col_letter(heads[-1][0]))

    if args.end:
        wanted = [nsdl_fpi.fortnight_end(datetime.strptime(args.end, "%Y-%m-%d").date())]
    else:
        wanted, e = [], nsdl_fpi.next_fortnight_end(last_end)
        while e <= date.today() - timedelta(days=1):
            wanted.append(e)
            e = nsdl_fpi.next_fortnight_end(e)
    wanted = [e for e in wanted if e > last_end]
    if not wanted:
        log.info("Up to date.")
        return 0

    reports = []
    for e in wanted:
        try:
            rep = nsdl_fpi.fetch_fortnight(e)
            reports.append(rep)
            log.info("NSDL %s: %d sectors, Grand Total %s", rep["label"], len(rep["sectors"]), f"{rep['grand_total']:,.0f}")
        except nsdl_fpi.NotPublished:
            log.info("NSDL report for %s not published yet (usually 2-4 days after) — will retry.", e)
            break
        except Exception as exc:
            log.error("NSDL report for %s could not be read: %s", e, exc)
            return 1
    if not reports:
        return 0

    out = Path(tempfile.mkstemp(suffix=".xlsx")[1])
    write_reports(WORKBOOK, out, reports)
    openpyxl.load_workbook(out).close()                     # sanity: file opens
    rotate_backup(WORKBOOK)
    shutil.move(str(out), WORKBOOK)
    try:
        sheet_history(WORKBOOK).to_csv(HISTORY, index=False)
    except Exception as exc:
        log.warning("history csv not written: %s", exc)
    log.info("Done: %s", ", ".join(r["label"] for r in reports))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--end", help="one fortnight, any date inside it (YYYY-MM-DD)")
    return run(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
