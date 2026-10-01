"""
participant_nse.py — download and parse the three NSE files the trainer's sheet uses.

Same URLs and headers as his Power Query (Section1.m inside the workbook):
  fao_participant_oi_DDMMYYYY.csv    -> participant OI      (Ctrl+Q/W/E/R, Ctrl+L)
  fao_participant_vol_DDMMYYYY.csv   -> participant volume  (Ctrl+K)
  fii_stats_DD-Mon-YYYY.xls          -> FII derivatives stats (Ctrl+P/O/I/U)
Plus the values he types by hand: Nifty / BankNifty close and FII / DII cash equity.

Raw files are kept under data/participant_raw/YYYY-MM-DD/ so a day can be rebuilt
without downloading again.
"""
from __future__ import annotations

import csv
import io
import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import requests

import config

log = logging.getLogger(__name__)

RAW_DIR = config.DATA_DIR / "participant_raw"
HEADERS = {"User-Agent": "Mozilla/5.0"}                  # exactly what his queries send
URL_OI = "https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_{d:%d%m%Y}.csv"
URL_VOL = "https://nsearchives.nseindia.com/content/nsccl/fao_participant_vol_{d:%d%m%Y}.csv"
URL_STATS = "https://nsearchives.nseindia.com/content/fo/fii_stats_{d:%d-%b-%Y}.xls"

OI_FIELDS = ["fut_idx_long", "fut_idx_short", "fut_stk_long", "fut_stk_short",
             "opt_idx_call_long", "opt_idx_put_long", "opt_idx_call_short", "opt_idx_put_short",
             "opt_stk_call_long", "opt_stk_put_long", "opt_stk_call_short", "opt_stk_put_short",
             "total_long", "total_short"]
PARTICIPANTS = {"CLIENT": "Client", "DII": "DII", "FII": "FII", "PRO": "Pro", "TOTAL": "TOTAL"}
STATS_KEYS = ["INDEX FUTURES", "INDEX OPTIONS", "STOCK FUTURES", "STOCK OPTIONS"]


class NotPublished(Exception):
    """NSE has no file for that date yet (evening lag) or at all (holiday)."""


# ───────────────────────────────── download ─────────────────────────────────
def _download(url: str, dest: Path, min_bytes: int = 200) -> Path:
    if dest.exists() and dest.stat().st_size >= min_bytes:
        return dest
    r = requests.get(url, headers=HEADERS, timeout=20)
    if r.status_code == 404 or len(r.content) < min_bytes:
        raise NotPublished(url)
    r.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(r.content)
    return dest


def download_day(d: date) -> dict[str, Path]:
    folder = RAW_DIR / f"{d:%Y-%m-%d}"
    return {
        "oi": _download(URL_OI.format(d=d), folder / f"fao_participant_oi_{d:%d%m%Y}.csv"),
        "vol": _download(URL_VOL.format(d=d), folder / f"fao_participant_vol_{d:%d%m%Y}.csv"),
        "stats": _download(URL_STATS.format(d=d), folder / f"fii_stats_{d:%d-%b-%Y}.xls", 2000),
    }


# ────────────────────────────────── parsing ─────────────────────────────────
def _num(s: Any) -> float:
    s = str(s).strip().replace(",", "").replace('"', "")
    if s in ("", "-", "nan", "None"):
        return 0.0
    return float(s)


def parse_participant_csv(path: Path) -> dict[str, dict[str, float]]:
    """Title line, header line, then Client / DII / FII / Pro / TOTAL rows, 15 columns."""
    text = path.read_bytes().decode("cp1252", errors="ignore")   # his query: Encoding=1252
    out: dict[str, dict[str, float]] = {}
    for row in csv.reader(io.StringIO(text)):
        if not row:
            continue
        key = row[0].strip().upper()
        if key in PARTICIPANTS and len(row) >= 15:
            out[PARTICIPANTS[key]] = {f: _num(v) for f, v in zip(OI_FIELDS, row[1:15])}
    missing = {"Client", "DII", "FII", "Pro"} - set(out)
    if missing:
        raise ValueError(f"{path.name}: rows missing {sorted(missing)}")
    return out


def parse_fii_stats(path: Path) -> dict[str, list[float]]:
    """sheet1 of fii_stats: label in column A, then buy qty, buy amt, sell qty, sell amt,
    OI qty, OI amt. Rows are located by label, not position, so NSE adding a row
    (as with NIFTYNXT50) does not shift anything."""
    engine = "xlrd" if path.suffix.lower() == ".xls" else None
    df = pd.read_excel(path, sheet_name=0, header=None, dtype=str, engine=engine)
    out: dict[str, list[float]] = {}
    for _, row in df.iterrows():
        label = str(row.iloc[0]).strip().upper()
        if not label or label == "NAN":
            continue
        vals = [row.iloc[i] for i in range(1, 7)] if len(row) >= 7 else []
        try:
            nums = [_num(v) for v in vals]
        except ValueError:
            continue                                         # header / text rows
        if len(nums) == 6:
            out[label] = nums
    missing = [k for k in STATS_KEYS if k not in out]
    if missing:
        raise ValueError(f"{path.name}: rows missing {missing}")
    return out


# ────────────────────────── values he types by hand ─────────────────────────
def nifty_close(d: date) -> float | None:
    """Nifty 50 close: last 5-min bar of that day (the file your 15:45 timer keeps complete)."""
    p = getattr(config, "CANDLE_5M_DIR", config.DATA_DIR / "candles_5m") / f"{d:%Y-%m-%d}.csv"
    try:
        df = pd.read_csv(p)
        if len(df) >= 60:
            return round(float(df["close"].iloc[-1]), 2)
    except Exception as exc:
        log.warning("nifty close from %s failed: %s", p, exc)
    return None


def banknifty_close(d: date) -> float | None:
    try:
        import dhan_client
        df = dhan_client.intraday(getattr(config, "DHAN_BANKNIFTY_SECURITY_ID", "25"),
                                  "IDX_I", "INDEX", days_back=7, interval="5")
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        day = df[df["timestamp"].dt.date == d]
        if len(day):
            return round(float(day["close"].iloc[-1]), 2)
    except Exception as exc:
        log.warning("banknifty close failed: %s", exc)
    return None


def fii_dii_cash(d: date) -> tuple[float | None, float | None]:
    """Provisional FII and DII cash-market net (₹ Cr) from NSE's FII/DII page API.
    Only answers for the latest session, so it is used for today only."""
    try:
        import nse_client
        client = nse_client.NSEClient()
        r = client._get("https://www.nseindia.com/api/fiidiiTradeReact", {})
        rows = r.json()
        fii = dii = None
        for row in rows:
            if datetime.strptime(row["date"], "%d-%b-%Y").date() != d:
                continue
            cat = row.get("category", "").upper()
            if cat.startswith("FII"):
                fii = round(float(row["netValue"]), 2)
            elif cat.startswith("DII"):
                dii = round(float(row["netValue"]), 2)
        return fii, dii
    except Exception as exc:
        log.warning("FII/DII cash fetch failed: %s", exc)
        return None, None


# ──────────────────────────────── one day's JSON ────────────────────────────
def build_day(d: date, overrides: dict | None = None, with_cash: bool = True) -> dict:
    files = download_day(d)
    oi = parse_participant_csv(files["oi"])
    vol = parse_participant_csv(files["vol"])
    stats = parse_fii_stats(files["stats"])
    fii_eq, dii_eq = fii_dii_cash(d) if with_cash else (None, None)
    day = {
        "date": f"{d:%Y-%m-%d}",
        "oi": oi,
        "vol": vol,
        "fii_stats": {k: stats[k] for k in STATS_KEYS},
        "fii_stats_all": stats,
        "nifty_close": nifty_close(d),
        "banknifty_close": banknifty_close(d),
        "fii_equity_cr": fii_eq,
        "dii_equity_cr": dii_eq,
        "fii_equity_final_prev_cr": None,
    }
    for k, v in (overrides or {}).items():
        if v is not None:
            day[k] = v
    (RAW_DIR / day["date"] / "day.json").write_text(json.dumps(day, indent=1))
    return day
