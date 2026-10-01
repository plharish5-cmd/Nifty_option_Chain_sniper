"""
nsdl_fpi.py — the two NSDL numbers the trainer copies by hand.

1. Daily FPI equity (final)  -> FII!BD of the PREVIOUS trading day
   NSDL > FPI Investments > Current month, row "Equity / Stock Exchange",
   column "Net Investment (Rs. Crore)". NSDL publishes it one day late: the value
   under reporting date 20-Feb belongs to the 19-Feb row. Occasionally NSDL skips a
   date and reports two days together (his example: 18+19 Feb shown under 20-Feb);
   it still goes into the one row just before the reporting date.

2. Fortnightly sector-wise FPI data -> 'FPI Fortnight Data.xlsx' Sheet2
   https://www.fpi.nsdl.co.in/web/StaticReports/Fortnightly_Sector_wise_FII_Investment_Data/
   FIIInvestSector_<Mon><DD><YYYY>.html  (e.g. FIIInvestSector_Aug312026.html)
   He copies the Equity "Net Investment" of the fortnight the report ends on
   (his Power Query column "Column51"), sector by sector down to Grand Total.

Only the standard library is used for HTML (no lxml / bs4 needed).
"""
from __future__ import annotations

import calendar
import logging
import re
from datetime import date, datetime, timedelta
from html.parser import HTMLParser

import requests

log = logging.getLogger("nsdl_fpi")

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36",
      "Accept": "text/html,application/xhtml+xml"}
DAILY_URLS = ["https://www.fpi.nsdl.co.in/web/Reports/Monthly.aspx",     # "Current month"
              "https://www.fpi.nsdl.co.in/web/Reports/Latest.aspx"]      # "Latest" (fallback)
FORTNIGHT_URL = ("https://www.fpi.nsdl.co.in/web/StaticReports/"
                 "Fortnightly_Sector_wise_FII_Investment_Data/FIIInvestSector_{tag}.html")


class NotPublished(Exception):
    pass


# ─────────────────────────────── HTML tables ────────────────────────────────
class _Tables(HTMLParser):
    """Collect every <table> as a grid of cell texts, honouring rowspan/colspan."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._stack: list[dict] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table":
            self._stack.append({"rows": [], "row": None, "cell": None, "spans": {}})
        elif not self._stack:
            return
        elif tag == "tr":
            self._stack[-1]["row"] = []
        elif tag in ("td", "th"):
            t = self._stack[-1]
            if t["row"] is None:
                t["row"] = []
            t["cell"] = {"text": [], "rs": _int(a.get("rowspan")), "cs": _int(a.get("colspan"))}
        elif tag == "br" and self._stack[-1]["cell"] is not None:
            self._stack[-1]["cell"]["text"].append(" ")

    def handle_data(self, data):
        if self._stack and self._stack[-1]["cell"] is not None:
            self._stack[-1]["cell"]["text"].append(data)

    def handle_endtag(self, tag):
        if not self._stack:
            return
        t = self._stack[-1]
        if tag in ("td", "th") and t["cell"] is not None:
            t["row"].append(t["cell"])
            t["cell"] = None
        elif tag == "tr" and t["row"] is not None:
            t["rows"].append(t["row"])
            t["row"] = None
        elif tag == "table":
            self._stack.pop()
            self.tables.append(_grid(t["rows"]))


def _int(v) -> int:
    try:
        return max(1, int(v))
    except (TypeError, ValueError):
        return 1


def _grid(rows: list[list[dict]]) -> list[list[str]]:
    grid: list[list[str]] = []
    carry: dict[int, tuple[str, int]] = {}          # col -> (text, rows still to fill)
    for cells in rows:
        out: list[str] = []
        col = 0
        it = iter(cells)
        pending = next(it, None)
        while pending is not None or any(c >= col for c in carry):
            if col in carry:
                text, left = carry[col]
                out.append(text)
                if left <= 1:
                    del carry[col]
                else:
                    carry[col] = (text, left - 1)
                col += 1
                continue
            if pending is None:
                break
            text = re.sub(r"\s+", " ", "".join(pending["text"])).strip()
            for k in range(pending["cs"]):
                out.append(text)
                if pending["rs"] > 1:
                    carry[col + k] = (text, pending["rs"] - 1)
            col += pending["cs"]
            pending = next(it, None)
        grid.append(out)
    return grid


def tables(html: str) -> list[list[list[str]]]:
    p = _Tables()
    p.feed(html)
    return p.tables


def num(s: str) -> float | None:
    s = (s or "").replace(",", "").replace("−", "-").strip()
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    try:
        return float(s)
    except ValueError:
        return None


def _get(url: str, timeout: int = 40) -> str:
    r = requests.get(url, headers=UA, timeout=timeout)
    if r.status_code == 404:
        raise NotPublished(url)
    r.raise_for_status()
    text = r.text
    if "Page Not Found" in text[:3000] or len(text) < 500:
        raise NotPublished(url)
    return text


# ─────────────────────────── 1) daily FPI equity ────────────────────────────
DATE_RX = re.compile(r"^\d{1,2}-[A-Za-z]{3}-\d{4}$")


def parse_daily(html: str) -> dict[date, float]:
    """{reporting date: Equity / Stock Exchange net investment ₹ Cr}."""
    out: dict[date, float] = {}
    for grid in tables(html):
        net_col = None
        for row in grid:
            low = [c.lower() for c in row]
            if net_col is None:
                for i, c in enumerate(low):
                    if "net investment" in c and ("crore" in c or "rs" in c):
                        net_col = i
                        break
                continue
            if len(row) <= net_col:
                continue
            d = next((c for c in row if DATE_RX.match(c)), None)
            if not d:
                continue
            if any(c.strip().lower() == "equity" for c in row[:3]) and \
               any("stock exchange" in c for c in low[:4]):
                v = num(row[net_col])
                if v is not None:
                    out[datetime.strptime(d, "%d-%b-%Y").date()] = round(v, 2)
    return out


def daily_equity() -> dict[date, float]:
    """Merged from the Current-month and Latest pages."""
    merged: dict[date, float] = {}
    errors = []
    for url in DAILY_URLS:
        try:
            got = parse_daily(_get(url))
            log.info("NSDL %s: %d dates", url.rsplit("/", 1)[-1], len(got))
            for k, v in got.items():
                merged.setdefault(k, v)
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    if not merged and errors:
        log.warning("NSDL daily FPI not read: %s", " | ".join(errors))
    return merged


def assign_to_rows(nsdl: dict[date, float], sheet_dates: list[date]) -> dict[date, float]:
    """His rule: value reported on date R goes to the last sheet date before R."""
    out: dict[date, float] = {}
    ds = sorted(sheet_dates)
    for rep, v in sorted(nsdl.items()):
        prev = [d for d in ds if d < rep]
        if prev:
            out[prev[-1]] = v
    return out


# ──────────────────────── 2) fortnightly sector data ────────────────────────
def fortnight_end(d: date) -> date:
    """End of the fortnight containing d (15th or month end)."""
    return date(d.year, d.month, 15) if d.day <= 15 else \
        date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def next_fortnight_end(end: date) -> date:
    return fortnight_end(end + timedelta(days=1))


def fortnight_tag(end: date) -> str:
    return f"{calendar.month_abbr[end.month]}{end.day:02d}{end.year}"     # Excel TEXT(d,"MMMDDYYYY")


def fortnight_label(end: date) -> str:
    """His short header style: 'Aug 1-15', 'Aug 16-31'."""
    m = calendar.month_abbr[end.month]
    return f"{m} 1-15" if end.day == 15 else f"{m} 16-{end.day}"


def parse_fortnight(html: str) -> dict:
    """Return {'sectors': {name: net equity ₹Cr of the report's own fortnight},
               'grand_total': float, 'auc_end': {name: ₹Cr}, 'period': str}."""
    best = None
    for grid in tables(html):
        if any(len(r) > 1 and r[1].strip().lower() == "sectors" for r in grid):
            best = grid
            break
    if best is None:
        raise ValueError("sector table not found")
    width = max(len(r) for r in best)
    hdr_net = next((r for r in best if sum("net investment" in c.lower() for c in r) >= 2), None)
    col = None
    period = ""
    if hdr_net:
        # blocks: AUC start | Net previous fortnight | Net this fortnight | AUC end
        starts = []
        for i, c in enumerate(hdr_net):
            if "net investment" in c.lower() and (i == 0 or hdr_net[i - 1] != c):
                starts.append(i)
        if len(starts) >= 2:
            col = starts[1]
            period = hdr_net[col]
        elif len(starts) == 1:                               # 3rd block title blank (as his Table 0 shows)
            col = starts[0] + (starts[0] - 2)                # block size = start of block 2 - 2
    if col is None:                                          # fixed layout fallback (Power Query Column51)
        col = 2 + 2 * ((width - 2) // 4)
    block = col - 2 - ((col - 2) // 2)                       # = block size when col is block 3
    auc_col = col + block
    sectors, auc = {}, {}
    total = None
    for r in best:
        if len(r) <= col or len(r) < 2:
            continue
        name = r[1].strip()
        v = num(r[col])
        if not name or v is None:
            continue
        if name.lower() == "grand total":
            total = v
        elif (r[0].strip().isdigit()):
            sectors[name] = v
            if len(r) > auc_col:
                auc[name] = num(r[auc_col])
    if not sectors:
        raise ValueError("no sector rows parsed")
    s = sum(sectors.values())
    if total is not None and abs(s - total) > max(5.0, 0.01 * abs(total)):
        log.warning("sector sum %.0f differs from Grand Total %.0f", s, total)
    return {"sectors": sectors, "grand_total": total if total is not None else s,
            "auc_end": auc, "period": period}


def fetch_fortnight(end: date) -> dict:
    url = FORTNIGHT_URL.format(tag=fortnight_tag(end))
    rep = parse_fortnight(_get(url))
    rep["end"] = end
    rep["url"] = url
    rep["label"] = fortnight_label(end)
    return rep
