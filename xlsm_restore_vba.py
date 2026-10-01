"""
xlsm_restore_vba.py — put the trainer's original macro project back into a workbook
that LibreOffice has saved.

LibreOffice keeps the macro code but drops the keyboard-shortcut bindings
(Ctrl+Q/W/E/R/K/P/O/I/U/L/N/B) and the sheet code names. This copies his original
xl/vbaProject.bin back in and restores the code names (ThisWorkbook, Sheet1…Sheet19)
by sheet name, so Excel sees exactly his macro project again.
"""
from __future__ import annotations

import re
import shutil
import tempfile
import zipfile
from pathlib import Path


def _sheet_files(z: zipfile.ZipFile) -> dict[str, str]:
    """sheet name -> xl/worksheets/sheetN.xml"""
    wb = z.read("xl/workbook.xml").decode("utf-8")
    rels = z.read("xl/_rels/workbook.xml.rels").decode("utf-8")
    rid_target = {}
    for m in re.finditer(r"<Relationship\b[^>]*>", rels):
        tag = m.group(0)
        rid = re.search(r'Id="([^"]+)"', tag)
        tgt = re.search(r'Target="([^"]+)"', tag)
        if rid and tgt:
            t = tgt.group(1).lstrip("/")
            rid_target[rid.group(1)] = t if t.startswith("xl/") else "xl/" + t
    out = {}
    for m in re.finditer(r"<sheet\b[^>]*/>", wb):
        tag = m.group(0)
        name = re.search(r'name="([^"]+)"', tag).group(1)
        rid = re.search(r'r:id="([^"]+)"', tag).group(1)
        out[name] = rid_target[rid]
    return out


def _code_names(z: zipfile.ZipFile) -> dict[str, str]:
    names = {}
    for name, path in _sheet_files(z).items():
        m = re.search(r'<sheetPr\b[^>]*codeName="([^"]+)"', z.read(path).decode("utf-8", "ignore"))
        if m:
            names[name] = m.group(1)
    return names


def _set_code_name(xml: str, code: str) -> str:
    if re.search(r"<sheetPr\b[^>]*codeName=", xml):
        return re.sub(r'(<sheetPr\b[^>]*codeName=")[^"]*(")', rf"\g<1>{code}\2", xml, count=1)
    if re.search(r"<sheetPr\b", xml):
        return re.sub(r"<sheetPr\b", f'<sheetPr codeName="{code}"', xml, count=1)
    return re.sub(r"(<worksheet\b[^>]*>)", rf'\1<sheetPr codeName="{code}"/>', xml, count=1)


def restore(original: Path | str, saved: Path | str) -> dict:
    original, saved = Path(original), Path(saved)
    with zipfile.ZipFile(original) as zo:
        vba = zo.read("xl/vbaProject.bin")
        codes = _code_names(zo)
        wb_code = re.search(r'<workbookPr\b[^>]*codeName="([^"]+)"', zo.read("xl/workbook.xml").decode())
        wb_code = wb_code.group(1) if wb_code else "ThisWorkbook"
    tmp = Path(tempfile.mkstemp(suffix=".xlsm")[1])
    with zipfile.ZipFile(saved) as zs, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zt:
        files = _sheet_files(zs)
        by_path = {p: codes[n] for n, p in files.items() if n in codes}
        for item in zs.infolist():
            data = zs.read(item.filename)
            if item.filename == "xl/vbaProject.bin":
                data = vba
            elif item.filename == "xl/workbook.xml":
                x = data.decode("utf-8")
                if "codeName=" not in x.split("<workbookPr", 1)[-1].split(">", 1)[0]:
                    x = re.sub(r"<workbookPr\b", f'<workbookPr codeName="{wb_code}"', x, count=1)
                data = x.encode("utf-8")
            elif item.filename in by_path:
                data = _set_code_name(data.decode("utf-8"), by_path[item.filename]).encode("utf-8")
            zt.writestr(item, data)
    shutil.move(str(tmp), saved)
    return {"sheets_named": len(by_path), "vba_bytes": len(vba)}


def extend_chart_ranges(saved: Path | str, extends: list[tuple[str, int, int]]) -> int:
    """Stretch chart ranges that ended on a tab's old last row, e.g.
    'OI vs Spot'!$B$2:$B$1695 -> $B$1696, by editing the saved chart XML text
    (keeps his series order, axes and colours exactly)."""
    if not extends:
        return 0
    saved = Path(saved)
    tmp = Path(tempfile.mkstemp(suffix=".xlsm")[1])
    changed = 0
    with zipfile.ZipFile(saved) as zs, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zt:
        for item in zs.infolist():
            data = zs.read(item.filename)
            if re.match(r"xl/charts/chart\d+\.xml$", item.filename):
                x = data.decode("utf-8")
                for tab, old, new in extends:
                    esc = tab.replace("'", "&apos;")
                    for name in {f"&apos;{esc}&apos;", f"'{tab}'", tab}:
                        pat = re.compile(re.escape(name) + r"(!\$[A-Z]+\$\d+:\$[A-Z]+\$)" + str(old) + r"(?!\d)")
                        x, k = pat.subn(lambda m: name + m.group(1) + str(new), x)
                        changed += k
                data = x.encode("utf-8")
            zt.writestr(item, data)
    shutil.move(str(tmp), saved)
    return changed
