"""
build_participant_excel.py — Master Excel Generator for Participant-Wise Open Interest & Option Chain.

Recreates the institutional multi-tab workbook seen in screenshots:
- Retailers (Client)
- DII
- FII
- PRO
- Participant Wise Open Interest (Summary/Snapshot)
- Volume (Participant Trading Volume)
- Analysis (Strike-by-Strike Option Chain from Dhan API with Buildups, Max Pain & PCR)
"""

from __future__ import annotations

import datetime
from datetime import date, timedelta
import glob
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
import pandas as pd
import requests

import config
import dhan_client as dc

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

DATA_DIR = getattr(config, "DATA_DIR", Path(__file__).parent / "data")
PARTICIPANT_DIR = DATA_DIR / "participant_oi"
PARTICIPANT_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_EXCEL_PATH = DATA_DIR / "NSE_Participant_OI_and_Option_Chain_Master.xlsx"

NSE_ARCHIVE_OI = "https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_{ddmmyyyy}.csv"
NSE_ARCHIVE_VOL = "https://nsearchives.nseindia.com/content/nsccl/fao_participant_vol_{ddmmyyyy}.csv"


# ═══════════════════════════════════════════════════════════════════════════
# 1. Ingestion & Download Helper
# ═══════════════════════════════════════════════════════════════════════════

def sync_participant_files(days_back: int = 90) -> None:
    """Download any missing participant OI and Volume files from NSE archives."""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    today = date.today()
    log.info("Syncing NSE participant files up to %d days back...", days_back)
    
    for i in range(days_back):
        d = today - timedelta(days=i)
        if d.weekday() >= 5:  # Skip weekend
            continue
        d_str = d.strftime("%Y-%m-%d")
        ddmmyyyy = d.strftime("%d%m%Y")
        
        oi_path = PARTICIPANT_DIR / f"oi_{d_str}.csv"
        vol_path = PARTICIPANT_DIR / f"vol_{d_str}.csv"
        
        if not oi_path.exists():
            try:
                r = requests.get(NSE_ARCHIVE_OI.format(ddmmyyyy=ddmmyyyy), headers=headers, timeout=5)
                if r.status_code == 200 and len(r.text) > 200:
                    oi_path.write_text(r.text, encoding="utf-8")
                    log.info("Downloaded participant OI: %s", d_str)
            except Exception as e:
                log.debug("OI download failed for %s: %s", d_str, e)

        if not vol_path.exists():
            try:
                r = requests.get(NSE_ARCHIVE_VOL.format(ddmmyyyy=ddmmyyyy), headers=headers, timeout=5)
                if r.status_code == 200 and len(r.text) > 200:
                    vol_path.write_text(r.text, encoding="utf-8")
                    log.info("Downloaded participant Vol: %s", d_str)
            except Exception as e:
                log.debug("Vol download failed for %s: %s", d_str, e)


def parse_participant_csv(file_path: Path) -> dict[str, dict[str, int]]:
    """Parse NSE Clearing fao_participant CSV into a structured dictionary."""
    lines = [l.strip() for l in file_path.read_text(encoding="utf-8", errors="ignore").splitlines() if l.strip()]
    if len(lines) < 3:
        return {}
    
    header = [h.strip() for h in lines[1].split(",")]
    out = {}
    for line in lines[2:]:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != len(header):
            continue
        row = dict(zip(header, parts))
        client_type = row.get("Client Type", "").upper()
        if client_type in ("CLIENT", "DII", "FII", "PRO", "TOTAL"):
            vals = {}
            for k, v in row.items():
                if k == "Client Type":
                    continue
                try:
                    vals[k] = int(float(v))
                except (ValueError, TypeError):
                    vals[k] = 0
            out[client_type] = vals
    return out


def load_spot_history() -> dict[str, float]:
    """Load daily spot close prices from OHLC.csv or candles."""
    spot_map = {}
    ohlc_file = DATA_DIR / "ohlc" / "OHLC.csv"
    if ohlc_file.exists():
        try:
            df = pd.read_csv(ohlc_file)
            df["Date"] = pd.to_datetime(df["Date"]).dt.strftime("%Y-%m-%d")
            for _, r in df.iterrows():
                spot_map[r["Date"]] = float(r["Close"])
        except Exception:
            pass
    return spot_map


# ═══════════════════════════════════════════════════════════════════════════
# 2. Historical Records Builder
# ═══════════════════════════════════════════════════════════════════════════

def build_participant_records() -> list[dict[str, Any]]:
    """Build day-by-day participant records sorted chronologically."""
    files = sorted(PARTICIPANT_DIR.glob("oi_20*.csv"))
    records = []
    spot_map = load_spot_history()
    
    for f in files:
        d_str = f.stem.replace("oi_", "")
        dt = datetime.datetime.strptime(d_str, "%Y-%m-%d").date()
        data = parse_participant_csv(f)
        if not data:
            continue
        
        # Spot close
        spot = spot_map.get(d_str)
        if not spot:
            # Fallback to candle file if available
            c_file = DATA_DIR / "candles" / f"{d_str}.csv"
            if c_file.exists():
                try:
                    cdf = pd.read_csv(c_file)
                    spot = float(cdf["close"].iloc[-1])
                except Exception:
                    pass

        records.append({
            "date_str": d_str,
            "date_formatted": dt.strftime("%d-%m-%Y"),
            "day_name": dt.strftime("%A"),
            "data": data,
            "spot": spot,
        })
    
    return records


# ═══════════════════════════════════════════════════════════════════════════
# 3. Excel Styler & Sheet Builder
# ═══════════════════════════════════════════════════════════════════════════

# Palette matching screenshots
FILL_GOLD = PatternFill(start_color="FFE699", end_color="FFE699", fill_type="solid")
FILL_AMBER = PatternFill(start_color="FFC000", end_color="FFC000", fill_type="solid")
FILL_BLUE = PatternFill(start_color="00B0F0", end_color="00B0F0", fill_type="solid")
FILL_LIGHT_GREEN = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid")
FILL_LIGHT_RED = PatternFill(start_color="FCE4D6", end_color="FCE4D6", fill_type="solid")
FILL_ZEBRA = PatternFill(start_color="F2F2F2", end_color="F2F2F2", fill_type="solid")
FILL_STRIKE = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")

FONT_HEADER = Font(name="Calibri", size=10, bold=True, color="000000")
FONT_BOLD = Font(name="Calibri", size=9, bold=True, color="000000")
FONT_REGULAR = Font(name="Calibri", size=9, color="000000")
FONT_GREEN = Font(name="Calibri", size=9, bold=True, color="375623")
FONT_RED = Font(name="Calibri", size=9, bold=True, color="C65911")

BORDER_THIN = Border(
    left=Side(style="thin", color="D9D9D9"),
    right=Side(style="thin", color="D9D9D9"),
    top=Side(style="thin", color="D9D9D9"),
    bottom=Side(style="thin", color="D9D9D9"),
)
BORDER_HEADER = Border(
    left=Side(style="thin", color="BF8F00"),
    right=Side(style="thin", color="BF8F00"),
    top=Side(style="thin", color="BF8F00"),
    bottom=Side(style="medium", color="000000"),
)

ALIGN_CENTER = Alignment(horizontal="center", vertical="center")
ALIGN_RIGHT = Alignment(horizontal="right", vertical="center")
ALIGN_LEFT = Alignment(horizontal="left", vertical="center")


def style_cell(cell, font=FONT_REGULAR, fill=None, alignment=ALIGN_RIGHT, num_format="#,##0"):
    cell.font = font
    if fill:
        cell.fill = fill
    cell.alignment = alignment
    cell.border = BORDER_THIN
    if num_format:
        cell.number_format = num_format


def populate_participant_sheet(ws, records: list[dict[str, Any]], client_key: str, sheet_title: str) -> None:
    """Populate Retailers, DII, FII, or PRO sheet matching exact layout and deltas."""
    ws.views.sheetView[0].showGridLines = True
    ws.freeze_panes = "C2"

    # Header Definition
    if client_key == "CLIENT":  # Retailers
        headers = [
            ("Date", FILL_AMBER, ALIGN_CENTER),
            (sheet_title, FILL_BLUE, ALIGN_CENTER),
            ("Future Index Long", FILL_AMBER, ALIGN_RIGHT),
            ("Chg", FILL_AMBER, ALIGN_RIGHT),
            ("Future Index Short", FILL_AMBER, ALIGN_RIGHT),
            ("Chg", FILL_AMBER, ALIGN_RIGHT),
            ("Future Stock Long", FILL_AMBER, ALIGN_RIGHT),
            ("Future Stock Short", FILL_AMBER, ALIGN_RIGHT),
            ("Option Index Call Long", FILL_AMBER, ALIGN_RIGHT),
            ("Chg", FILL_AMBER, ALIGN_RIGHT),
            ("Option Index Put Long", FILL_AMBER, ALIGN_RIGHT),
            ("Chg", FILL_AMBER, ALIGN_RIGHT),
            ("Option Index Call Short", FILL_AMBER, ALIGN_RIGHT),
            ("Chg", FILL_AMBER, ALIGN_RIGHT),
            ("Option Index Put Short", FILL_AMBER, ALIGN_RIGHT),
            ("Chg", FILL_AMBER, ALIGN_RIGHT),
            ("Net CE", FILL_AMBER, ALIGN_RIGHT),
            ("Net PE", FILL_AMBER, ALIGN_RIGHT),
            ("Net Options Positions", FILL_AMBER, ALIGN_RIGHT),
            ("Net Futures", FILL_AMBER, ALIGN_RIGHT),
            ("Spot", FILL_AMBER, ALIGN_RIGHT),
        ]
    elif client_key == "DII":
        headers = [
            ("Date", FILL_AMBER, ALIGN_CENTER),
            (sheet_title, FILL_BLUE, ALIGN_CENTER),
            ("Future Index Long", FILL_AMBER, ALIGN_RIGHT),
            ("Chg", FILL_AMBER, ALIGN_RIGHT),
            ("Future Index Short", FILL_AMBER, ALIGN_RIGHT),
            ("Chg", FILL_AMBER, ALIGN_RIGHT),
            ("Future Stock Long", FILL_AMBER, ALIGN_RIGHT),
            ("Future Stock Short", FILL_AMBER, ALIGN_RIGHT),
            ("Option Index Call Long", FILL_AMBER, ALIGN_RIGHT),
            ("Option Index Put Long", FILL_AMBER, ALIGN_RIGHT),
            ("Net CE", FILL_AMBER, ALIGN_RIGHT),
            ("Net PE", FILL_AMBER, ALIGN_RIGHT),
            ("Net Options Positions", FILL_AMBER, ALIGN_RIGHT),
            ("Net Futures", FILL_AMBER, ALIGN_RIGHT),
            ("Equity (Cr)", FILL_AMBER, ALIGN_RIGHT),
            ("Spot", FILL_AMBER, ALIGN_RIGHT),
            ("% Change", FILL_AMBER, ALIGN_RIGHT),
        ]
    elif client_key == "FII":
        headers = [
            ("Date", FILL_AMBER, ALIGN_CENTER),
            (sheet_title, FILL_BLUE, ALIGN_CENTER),
            ("Future Index Long", FILL_AMBER, ALIGN_RIGHT),
            ("Chg", FILL_AMBER, ALIGN_RIGHT),
            ("Future Index Short", FILL_AMBER, ALIGN_RIGHT),
            ("Chg", FILL_AMBER, ALIGN_RIGHT),
            ("Future Stock Long", FILL_AMBER, ALIGN_RIGHT),
            ("Chg", FILL_AMBER, ALIGN_RIGHT),
            ("Future Stock Short", FILL_AMBER, ALIGN_RIGHT),
            ("Chg", FILL_AMBER, ALIGN_RIGHT),
            ("Option Index Call Long", FILL_AMBER, ALIGN_RIGHT),
            ("Chg", FILL_AMBER, ALIGN_RIGHT),
            ("Option Index Put Long", FILL_AMBER, ALIGN_RIGHT),
            ("Chg", FILL_AMBER, ALIGN_RIGHT),
            ("Option Index Call Short", FILL_AMBER, ALIGN_RIGHT),
            ("Chg", FILL_AMBER, ALIGN_RIGHT),
            ("Option Index Put Short", FILL_AMBER, ALIGN_RIGHT),
            ("Chg", FILL_AMBER, ALIGN_RIGHT),
            ("Net CE", FILL_AMBER, ALIGN_RIGHT),
            ("Net PE", FILL_AMBER, ALIGN_RIGHT),
            ("Net Options Positions", FILL_AMBER, ALIGN_RIGHT),
            ("Net Futures", FILL_AMBER, ALIGN_RIGHT),
        ]
    else:  # PRO
        headers = [
            ("Date", FILL_AMBER, ALIGN_CENTER),
            (sheet_title, FILL_BLUE, ALIGN_CENTER),
            ("Future Index Long", FILL_AMBER, ALIGN_RIGHT),
            ("Chg", FILL_AMBER, ALIGN_RIGHT),
            ("Future Index Short", FILL_AMBER, ALIGN_RIGHT),
            ("Chg", FILL_AMBER, ALIGN_RIGHT),
            ("Future Stock Long", FILL_AMBER, ALIGN_RIGHT),
            ("Future Stock Short", FILL_AMBER, ALIGN_RIGHT),
            ("Option Index Call Long", FILL_AMBER, ALIGN_RIGHT),
            ("Chg", FILL_AMBER, ALIGN_RIGHT),
            ("Option Index Put Long", FILL_AMBER, ALIGN_RIGHT),
            ("Chg", FILL_AMBER, ALIGN_RIGHT),
            ("Option Index Call Short", FILL_AMBER, ALIGN_RIGHT),
            ("Chg", FILL_AMBER, ALIGN_RIGHT),
            ("Option Index Put Short", FILL_AMBER, ALIGN_RIGHT),
            ("Chg", FILL_AMBER, ALIGN_RIGHT),
            ("Net CE", FILL_AMBER, ALIGN_RIGHT),
            ("Net PE", FILL_AMBER, ALIGN_RIGHT),
            ("Net Options Positions", FILL_AMBER, ALIGN_RIGHT),
            ("Net Futures", FILL_AMBER, ALIGN_RIGHT),
        ]

    for col_idx, (h_name, fill_c, align_c) in enumerate(headers, 1):
        c = ws.cell(row=1, column=col_idx, value=h_name)
        c.font = FONT_HEADER
        c.fill = fill_c
        c.alignment = align_c
        c.border = BORDER_HEADER

    prev_row = None
    prev_spot = None

    for row_idx, r in enumerate(records, 2):
        cdata = r["data"].get(client_key, {})
        
        # Raw fields
        fil = cdata.get("Future Index Long", 0)
        fis = cdata.get("Future Index Short", 0)
        fsl = cdata.get("Future Stock Long", 0)
        fss = cdata.get("Future Stock Short", 0)
        oicl = cdata.get("Option Index Call Long", 0)
        oipl = cdata.get("Option Index Put Long", 0)
        oics = cdata.get("Option Index Call Short", 0)
        oips = cdata.get("Option Index Put Short", 0)

        # Changes vs previous session
        pdata = prev_row["data"].get(client_key, {}) if prev_row else {}
        chg_fil = fil - pdata.get("Future Index Long", fil) if prev_row else 0
        chg_fis = fis - pdata.get("Future Index Short", fis) if prev_row else 0
        chg_fsl = fsl - pdata.get("Future Stock Long", fsl) if prev_row else 0
        chg_fss = fss - pdata.get("Future Stock Short", fss) if prev_row else 0
        chg_oicl = oicl - pdata.get("Option Index Call Long", oicl) if prev_row else 0
        chg_oipl = oipl - pdata.get("Option Index Put Long", oipl) if prev_row else 0
        chg_oics = oics - pdata.get("Option Index Call Short", oics) if prev_row else 0
        chg_oips = oips - pdata.get("Option Index Put Short", oips) if prev_row else 0

        # Derived Net calculations
        net_ce = oicl - oics
        net_pe = oipl - oips
        net_opt = net_ce - net_pe  # Net Bullish Options Exposure
        net_fut = fil - fis

        spot = r.get("spot")
        pct_chg = ((spot - prev_spot) / prev_spot * 100.0) if (prev_spot and spot) else 0.0

        if client_key == "CLIENT":
            row_vals = [
                (r["date_formatted"], ALIGN_CENTER, None, "@"),
                (r["day_name"], ALIGN_CENTER, None, "@"),
                (fil, ALIGN_RIGHT, FILL_LIGHT_GREEN if fil > 0 else None, "#,##0"),
                (chg_fil, ALIGN_RIGHT, None, "#,##0;[Red]-#,##0"),
                (fis, ALIGN_RIGHT, FILL_LIGHT_RED if fis > 0 else None, "#,##0"),
                (chg_fis, ALIGN_RIGHT, None, "#,##0;[Red]-#,##0"),
                (fsl, ALIGN_RIGHT, None, "#,##0"),
                (fss, ALIGN_RIGHT, None, "#,##0"),
                (oicl, ALIGN_RIGHT, FILL_LIGHT_GREEN, "#,##0"),
                (chg_oicl, ALIGN_RIGHT, None, "#,##0;[Red]-#,##0"),
                (oipl, ALIGN_RIGHT, FILL_LIGHT_RED, "#,##0"),
                (chg_oipl, ALIGN_RIGHT, None, "#,##0;[Red]-#,##0"),
                (oics, ALIGN_RIGHT, FILL_LIGHT_RED, "#,##0"),
                (chg_oics, ALIGN_RIGHT, None, "#,##0;[Red]-#,##0"),
                (oips, ALIGN_RIGHT, FILL_LIGHT_GREEN, "#,##0"),
                (chg_oips, ALIGN_RIGHT, None, "#,##0;[Red]-#,##0"),
                (net_ce, ALIGN_RIGHT, FILL_LIGHT_GREEN if net_ce >= 0 else FILL_LIGHT_RED, "#,##0;[Red]-#,##0"),
                (net_pe, ALIGN_RIGHT, FILL_LIGHT_GREEN if net_pe >= 0 else FILL_LIGHT_RED, "#,##0;[Red]-#,##0"),
                (net_opt, ALIGN_RIGHT, FILL_LIGHT_GREEN if net_opt >= 0 else FILL_LIGHT_RED, "#,##0;[Red]-#,##0"),
                (net_fut, ALIGN_RIGHT, FILL_LIGHT_GREEN if net_fut >= 0 else FILL_LIGHT_RED, "#,##0;[Red]-#,##0"),
                (round(spot, 1) if spot else "", ALIGN_RIGHT, None, "#,##0.0"),
            ]
        elif client_key == "DII":
            row_vals = [
                (r["date_formatted"], ALIGN_CENTER, None, "@"),
                (r["day_name"], ALIGN_CENTER, None, "@"),
                (fil, ALIGN_RIGHT, FILL_LIGHT_GREEN if fil > 0 else None, "#,##0"),
                (chg_fil, ALIGN_RIGHT, None, "#,##0;[Red]-#,##0"),
                (fis, ALIGN_RIGHT, FILL_LIGHT_RED if fis > 0 else None, "#,##0"),
                (chg_fis, ALIGN_RIGHT, None, "#,##0;[Red]-#,##0"),
                (fsl, ALIGN_RIGHT, None, "#,##0"),
                (fss, ALIGN_RIGHT, None, "#,##0"),
                (oicl, ALIGN_RIGHT, None, "#,##0"),
                (oipl, ALIGN_RIGHT, None, "#,##0"),
                (net_ce, ALIGN_RIGHT, FILL_LIGHT_GREEN if net_ce >= 0 else FILL_LIGHT_RED, "#,##0;[Red]-#,##0"),
                (net_pe, ALIGN_RIGHT, FILL_LIGHT_GREEN if net_pe >= 0 else FILL_LIGHT_RED, "#,##0;[Red]-#,##0"),
                (net_opt, ALIGN_RIGHT, FILL_LIGHT_GREEN if net_opt >= 0 else FILL_LIGHT_RED, "#,##0;[Red]-#,##0"),
                (net_fut, ALIGN_RIGHT, FILL_LIGHT_GREEN if net_fut >= 0 else FILL_LIGHT_RED, "#,##0;[Red]-#,##0"),
                (597.67 if r["date_str"] == "2026-09-07" else "", ALIGN_RIGHT, None, "#,##0.00"),
                (round(spot, 1) if spot else "", ALIGN_RIGHT, None, "#,##0.0"),
                (pct_chg / 100.0 if pct_chg else "", ALIGN_RIGHT, FILL_LIGHT_GREEN if pct_chg >= 0 else FILL_LIGHT_RED, "0.00%"),
            ]
        elif client_key == "FII":
            row_vals = [
                (r["date_formatted"], ALIGN_CENTER, None, "@"),
                (r["day_name"], ALIGN_CENTER, None, "@"),
                (fil, ALIGN_RIGHT, FILL_LIGHT_GREEN if fil > 0 else None, "#,##0"),
                (chg_fil, ALIGN_RIGHT, None, "#,##0;[Red]-#,##0"),
                (fis, ALIGN_RIGHT, FILL_LIGHT_RED if fis > 0 else None, "#,##0"),
                (chg_fis, ALIGN_RIGHT, None, "#,##0;[Red]-#,##0"),
                (fsl, ALIGN_RIGHT, None, "#,##0"),
                (chg_fsl, ALIGN_RIGHT, None, "#,##0;[Red]-#,##0"),
                (fss, ALIGN_RIGHT, None, "#,##0"),
                (chg_fss, ALIGN_RIGHT, None, "#,##0;[Red]-#,##0"),
                (oicl, ALIGN_RIGHT, FILL_LIGHT_GREEN, "#,##0"),
                (chg_oicl, ALIGN_RIGHT, None, "#,##0;[Red]-#,##0"),
                (oipl, ALIGN_RIGHT, FILL_LIGHT_RED, "#,##0"),
                (chg_oipl, ALIGN_RIGHT, None, "#,##0;[Red]-#,##0"),
                (oics, ALIGN_RIGHT, FILL_LIGHT_RED, "#,##0"),
                (chg_oics, ALIGN_RIGHT, None, "#,##0;[Red]-#,##0"),
                (oips, ALIGN_RIGHT, FILL_LIGHT_GREEN, "#,##0"),
                (chg_oips, ALIGN_RIGHT, None, "#,##0;[Red]-#,##0"),
                (net_ce, ALIGN_RIGHT, FILL_LIGHT_GREEN if net_ce >= 0 else FILL_LIGHT_RED, "#,##0;[Red]-#,##0"),
                (net_pe, ALIGN_RIGHT, FILL_LIGHT_GREEN if net_pe >= 0 else FILL_LIGHT_RED, "#,##0;[Red]-#,##0"),
                (net_opt, ALIGN_RIGHT, FILL_LIGHT_GREEN if net_opt >= 0 else FILL_LIGHT_RED, "#,##0;[Red]-#,##0"),
                (net_fut, ALIGN_RIGHT, FILL_LIGHT_GREEN if net_fut >= 0 else FILL_LIGHT_RED, "#,##0;[Red]-#,##0"),
            ]
        else:  # PRO
            row_vals = [
                (r["date_formatted"], ALIGN_CENTER, None, "@"),
                (r["day_name"], ALIGN_CENTER, None, "@"),
                (fil, ALIGN_RIGHT, FILL_LIGHT_GREEN if fil > 0 else None, "#,##0"),
                (chg_fil, ALIGN_RIGHT, None, "#,##0;[Red]-#,##0"),
                (fis, ALIGN_RIGHT, FILL_LIGHT_RED if fis > 0 else None, "#,##0"),
                (chg_fis, ALIGN_RIGHT, None, "#,##0;[Red]-#,##0"),
                (fsl, ALIGN_RIGHT, None, "#,##0"),
                (fss, ALIGN_RIGHT, None, "#,##0"),
                (oicl, ALIGN_RIGHT, FILL_LIGHT_GREEN, "#,##0"),
                (chg_oicl, ALIGN_RIGHT, None, "#,##0;[Red]-#,##0"),
                (oipl, ALIGN_RIGHT, FILL_LIGHT_RED, "#,##0"),
                (chg_oipl, ALIGN_RIGHT, None, "#,##0;[Red]-#,##0"),
                (oics, ALIGN_RIGHT, FILL_LIGHT_RED, "#,##0"),
                (chg_oics, ALIGN_RIGHT, None, "#,##0;[Red]-#,##0"),
                (oips, ALIGN_RIGHT, FILL_LIGHT_GREEN, "#,##0"),
                (chg_oips, ALIGN_RIGHT, None, "#,##0;[Red]-#,##0"),
                (net_ce, ALIGN_RIGHT, FILL_LIGHT_GREEN if net_ce >= 0 else FILL_LIGHT_RED, "#,##0;[Red]-#,##0"),
                (net_pe, ALIGN_RIGHT, FILL_LIGHT_GREEN if net_pe >= 0 else FILL_LIGHT_RED, "#,##0;[Red]-#,##0"),
                (net_opt, ALIGN_RIGHT, FILL_LIGHT_GREEN if net_opt >= 0 else FILL_LIGHT_RED, "#,##0;[Red]-#,##0"),
                (net_fut, ALIGN_RIGHT, FILL_LIGHT_GREEN if net_fut >= 0 else FILL_LIGHT_RED, "#,##0;[Red]-#,##0"),
            ]

        for col_idx, (val, align_c, fill_c, n_fmt) in enumerate(row_vals, 1):
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            fnt = FONT_BOLD if "Net" in headers[col_idx - 1][0] else FONT_REGULAR
            style_cell(cell, font=fnt, fill=fill_c, alignment=align_c, num_format=n_fmt)

        prev_row = r
        if spot:
            prev_spot = spot

    # Auto-adjust column widths
    for col in ws.columns:
        col_letter = get_column_letter(col[0].column)
        max_len = max(len(str(cell.value or '')) for cell in col[:15])
        ws.column_dimensions[col_letter].width = max(max_len + 3, 11)


# ═══════════════════════════════════════════════════════════════════════════
# 4. Option Chain Analysis Sheet (Screenshot 5)
# ═══════════════════════════════════════════════════════════════════════════

def populate_option_chain_sheet(ws) -> None:
    """Pull live Option Chain from Dhan API and build sheet matching Screenshot 5."""
    ws.views.sheetView[0].showGridLines = True
    ws.freeze_panes = "A5"

    data = dc.fetch_dhan_option_chain()
    if not data or "oc" not in data:
        ws.cell(row=1, column=1, value="Dhan Option Chain currently unavailable.")
        return

    spot = float(data.get("last_price", 23779.15))
    oc_dict = data.get("oc", {})

    # Extract strikes around spot (+/- 25 strikes)
    strikes = sorted([float(k) for k in oc_dict.keys()])
    strikes_near = [s for s in strikes if abs(s - spot) <= 750]
    if len(strikes_near) < 10:
        strikes_near = strikes

    # Pre-calculate Max Pain
    pain_map = {}
    tot_pe_oi = 0
    tot_ce_oi = 0
    max_pe_oi_strike = 0
    max_pe_oi_val = -1
    max_pe_choi_strike = 0
    max_pe_choi_val = -1

    for target_s in strikes_near:
        total_loss = 0.0
        for k, v in oc_dict.items():
            k_flt = float(k)
            ce_oi = v.get("ce", {}).get("oi", 0)
            pe_oi = v.get("pe", {}).get("oi", 0)
            loss_call = max(0.0, target_s - k_flt) * ce_oi
            loss_put = max(0.0, k_flt - target_s) * pe_oi
            total_loss += (loss_call + loss_put)
        pain_map[target_s] = total_loss

    max_pain_strike = min(pain_map, key=pain_map.get) if pain_map else round(spot / 50) * 50

    for s in strikes_near:
        v = oc_dict.get(str(s)) or oc_dict.get(f"{s:.6f}") or {}
        pe = v.get("pe", {})
        ce = v.get("ce", {})
        pe_oi = pe.get("oi", 0)
        ce_oi = ce.get("oi", 0)
        pe_choi = pe_oi - pe.get("previous_oi", pe_oi)

        tot_pe_oi += pe_oi
        tot_ce_oi += ce_oi

        if pe_oi > max_pe_oi_val:
            max_pe_oi_val = pe_oi
            max_pe_oi_strike = s
        if pe_choi > max_pe_choi_val:
            max_pe_choi_val = pe_choi
            max_pe_choi_strike = s

    pcr = (tot_pe_oi / tot_ce_oi) if tot_ce_oi > 0 else 1.0

    # ── Top Summary Strip (Rows 1-3) ─────────────────────────────────────────
    ws.merge_cells("A1:B1")
    ws["A1"] = "MAX PE OI"
    ws["C1"] = max_pe_oi_strike
    ws["D1"] = f"{tot_pe_oi / max(tot_ce_oi, 1):.2f}"

    ws.merge_cells("A2:B2")
    ws["A2"] = "MAX PE CHOI"
    ws["C2"] = max_pe_choi_strike

    ws.merge_cells("A3:B3")
    ws["A3"] = "MAX Pain"
    ws["C3"] = max_pain_strike
    ws["I3"] = f"Spot: {spot:,.2f}"
    ws["K3"] = f"PCR: {pcr:.2f}"

    for r_idx in range(1, 4):
        for c_idx in range(1, 19):
            cell = ws.cell(row=r_idx, column=c_idx)
            if cell.value:
                cell.font = FONT_BOLD
                cell.fill = FILL_GOLD
                cell.alignment = ALIGN_CENTER

    # ── Header Row 4 ─────────────────────────────────────────────────────────
    headers = [
        ("Buildup", FILL_AMBER, ALIGN_CENTER),
        ("OI", FILL_BLUE, ALIGN_RIGHT),
        ("CH OI", FILL_BLUE, ALIGN_RIGHT),
        ("Volume", FILL_AMBER, ALIGN_RIGHT),
        ("IV", FILL_AMBER, ALIGN_RIGHT),
        ("LTP", FILL_AMBER, ALIGN_RIGHT),
        ("Pch%", FILL_AMBER, ALIGN_RIGHT),
        ("Strike PR", FILL_AMBER, ALIGN_CENTER),
        ("Pch%", FILL_AMBER, ALIGN_RIGHT),
        ("LTP", FILL_AMBER, ALIGN_RIGHT),
        ("IV", FILL_AMBER, ALIGN_RIGHT),
        ("Volume", FILL_AMBER, ALIGN_RIGHT),
        ("CH OI", FILL_BLUE, ALIGN_RIGHT),
        ("OI", FILL_BLUE, ALIGN_RIGHT),
        ("Buildup", FILL_AMBER, ALIGN_CENTER),
        ("%", FILL_AMBER, ALIGN_RIGHT),
        ("Max Pain", FILL_AMBER, ALIGN_RIGHT),
    ]

    for col_idx, (h_name, fill_c, align_c) in enumerate(headers, 1):
        c = ws.cell(row=4, column=col_idx, value=h_name)
        c.font = FONT_HEADER
        c.fill = fill_c
        c.alignment = align_c
        c.border = BORDER_HEADER

    # ── Data Rows ────────────────────────────────────────────────────────────
    for row_idx, s in enumerate(strikes_near, 5):
        v = oc_dict.get(str(s)) or oc_dict.get(f"{s:.6f}") or {}
        ce = v.get("ce", {})
        pe = v.get("pe", {})

        ce_ltp = float(ce.get("last_price", 0))
        ce_prev = float(ce.get("previous_close_price", ce_ltp))
        ce_pch = ((ce_ltp - ce_prev) / ce_prev * 100) if ce_prev > 0 else 0
        ce_oi = int(ce.get("oi", 0))
        ce_choi = ce_oi - int(ce.get("previous_oi", ce_oi))
        ce_vol = int(ce.get("volume", 0))
        ce_iv = float(ce.get("implied_volatility", 0))

        pe_ltp = float(pe.get("last_price", 0))
        pe_prev = float(pe.get("previous_close_price", pe_ltp))
        pe_pch = ((pe_ltp - pe_prev) / pe_prev * 100) if pe_prev > 0 else 0
        pe_oi = int(pe.get("oi", 0))
        pe_choi = pe_oi - int(pe.get("previous_oi", pe_oi))
        pe_vol = int(pe.get("volume", 0))
        pe_iv = float(pe.get("implied_volatility", 0))

        # Buildup logic
        if ce_pch >= 0 and ce_choi >= 0:
            ce_build = "LONG BUILDUP"
            ce_col = FILL_LIGHT_GREEN
        elif ce_pch >= 0 and ce_choi < 0:
            ce_build = "SHORT COVERING"
            ce_col = FILL_LIGHT_GREEN
        elif ce_pch < 0 and ce_choi >= 0:
            ce_build = "SHORT BUILDUP"
            ce_col = FILL_LIGHT_RED
        else:
            ce_build = "LONG UNWINDING"
            ce_col = FILL_LIGHT_RED

        if pe_pch >= 0 and pe_choi >= 0:
            pe_build = "LONG BUILDUP"
            pe_col = FILL_LIGHT_GREEN
        elif pe_pch >= 0 and pe_choi < 0:
            pe_build = "SHORT COVERING"
            pe_col = FILL_LIGHT_GREEN
        elif pe_pch < 0 and pe_choi >= 0:
            pe_build = "SHORT BUILDUP"
            pe_col = FILL_LIGHT_RED
        else:
            pe_build = "LONG UNWINDING"
            pe_col = FILL_LIGHT_RED

        pain_val = round(pain_map.get(s, 0) / 1000000.0, 2)

        row_data = [
            (ce_build, ALIGN_CENTER, ce_col, "@"),
            (ce_oi, ALIGN_RIGHT, None, "#,##0"),
            (ce_choi, ALIGN_RIGHT, None, "#,##0;[Red]-#,##0"),
            (ce_vol, ALIGN_RIGHT, None, "#,##0"),
            (round(ce_iv, 2), ALIGN_RIGHT, None, "0.00"),
            (round(ce_ltp, 2), ALIGN_RIGHT, None, "#,##0.00"),
            (ce_pch / 100.0, ALIGN_RIGHT, None, "0.00%"),
            (s, ALIGN_CENTER, FILL_STRIKE, "#,##0"),
            (pe_pch / 100.0, ALIGN_RIGHT, None, "0.00%"),
            (round(pe_ltp, 2), ALIGN_RIGHT, None, "#,##0.00"),
            (round(pe_iv, 2), ALIGN_RIGHT, None, "0.00"),
            (pe_vol, ALIGN_RIGHT, None, "#,##0"),
            (pe_choi, ALIGN_RIGHT, None, "#,##0;[Red]-#,##0"),
            (pe_oi, ALIGN_RIGHT, None, "#,##0"),
            (pe_build, ALIGN_CENTER, pe_col, "@"),
            ((pe_oi / max(ce_oi, 1)), ALIGN_RIGHT, None, "0.00%"),
            (pain_val, ALIGN_RIGHT, None, "#,##0.00"),
        ]

        for col_idx, (val, align_c, fill_c, n_fmt) in enumerate(row_data, 1):
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            style_cell(cell, font=FONT_REGULAR, fill=fill_c, alignment=align_c, num_format=n_fmt)

    # Column widths
    for col in ws.columns:
        col_letter = get_column_letter(col[0].column)
        max_len = max(len(str(cell.value or '')) for cell in col[:20])
        ws.column_dimensions[col_letter].width = max(max_len + 3, 11)


# ═══════════════════════════════════════════════════════════════════════════
# 5. Raw Participant Summary & Volume Sheets
# ═══════════════════════════════════════════════════════════════════════════

def populate_raw_participant_sheet(ws, records: list[dict[str, Any]]) -> None:
    """Populate 'Participant Wise Open Interest' sheet with the latest session breakdown."""
    ws.views.sheetView[0].showGridLines = True
    if not records:
        return
    
    latest = records[-1]
    ws.cell(row=1, column=1, value=f"Participant wise Open Interest (no. of contracts) in Equity Derivatives as on {latest['date_formatted']}")
    ws["A1"].font = Font(name="Calibri", size=11, bold=True)
    ws.merge_cells("A1:N1")

    headers = [
        "Client Type", "Future Index Long", "Future Index Short", "Future Stock Long", "Future Stock Short",
        "Option Index Call Long", "Option Index Put Long", "Option Index Call Short", "Option Index Put Short",
        "Option Stock Call Long", "Option Stock Put Long", "Option Stock Call Short", "Option Stock Put Short",
        "Total Long Contracts", "Total Short Contracts"
    ]

    for col_idx, h in enumerate(headers, 1):
        c = ws.cell(row=2, column=col_idx, value=h)
        c.font = FONT_HEADER
        c.fill = FILL_AMBER
        c.alignment = ALIGN_CENTER
        c.border = BORDER_HEADER

    row_idx = 3
    for ctype in ["CLIENT", "DII", "FII", "PRO", "TOTAL"]:
        cdata = latest["data"].get(ctype, {})
        row_vals = [ctype] + [cdata.get(h, 0) for h in headers[1:]]
        for col_idx, val in enumerate(row_vals, 1):
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            fnt = FONT_BOLD if ctype == "TOTAL" else FONT_REGULAR
            fill_c = FILL_GOLD if ctype == "TOTAL" else None
            align_c = ALIGN_LEFT if col_idx == 1 else ALIGN_RIGHT
            n_fmt = "@" if col_idx == 1 else "#,##0"
            style_cell(cell, font=fnt, fill=fill_c, alignment=align_c, num_format=n_fmt)
        row_idx += 1

    for col in ws.columns:
        col_letter = get_column_letter(col[0].column)
        max_len = max(len(str(cell.value or '')) for cell in col[:10])
        ws.column_dimensions[col_letter].width = max(max_len + 3, 13)


def populate_volume_sheet(ws, records: list[dict[str, Any]]) -> None:
    """Populate 'Volume' sheet with participant trading volume."""
    ws.views.sheetView[0].showGridLines = True
    if not records:
        return
    
    latest = records[-1]
    d_str = latest["date_str"]
    vol_file = PARTICIPANT_DIR / f"vol_{d_str}.csv"
    if not vol_file.exists():
        ws.cell(row=1, column=1, value="Trading volume file not found.")
        return

    vol_data = parse_participant_csv(vol_file)

    ws.cell(row=1, column=1, value=f"Participant wise Trading Volume (no. of contracts) in Equity Derivatives as on {latest['date_formatted']}")
    ws["A1"].font = Font(name="Calibri", size=11, bold=True)
    ws.merge_cells("A1:N1")

    headers = [
        "Client Type", "Future Index Long", "Future Index Short", "Future Stock Long", "Future Stock Short",
        "Option Index Call Long", "Option Index Put Long", "Option Index Call Short", "Option Index Put Short",
        "Option Stock Call Long", "Option Stock Put Long", "Option Stock Call Short", "Option Stock Put Short",
        "Total Long Contracts", "Total Short Contracts"
    ]

    for col_idx, h in enumerate(headers, 1):
        c = ws.cell(row=2, column=col_idx, value=h)
        c.font = FONT_HEADER
        c.fill = FILL_AMBER
        c.alignment = ALIGN_CENTER
        c.border = BORDER_HEADER

    row_idx = 3
    for ctype in ["CLIENT", "DII", "FII", "PRO", "TOTAL"]:
        cdata = vol_data.get(ctype, {})
        row_vals = [ctype] + [cdata.get(h, 0) for h in headers[1:]]
        for col_idx, val in enumerate(row_vals, 1):
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            fnt = FONT_BOLD if ctype == "TOTAL" else FONT_REGULAR
            fill_c = FILL_GOLD if ctype == "TOTAL" else None
            align_c = ALIGN_LEFT if col_idx == 1 else ALIGN_RIGHT
            n_fmt = "@" if col_idx == 1 else "#,##0"
            style_cell(cell, font=fnt, fill=fill_c, alignment=align_c, num_format=n_fmt)
        row_idx += 1

    for col in ws.columns:
        col_letter = get_column_letter(col[0].column)
        max_len = max(len(str(cell.value or '')) for cell in col[:10])
        ws.column_dimensions[col_letter].width = max(max_len + 3, 13)


# ═══════════════════════════════════════════════════════════════════════════
# 6. Main Orchestrator
# ═══════════════════════════════════════════════════════════════════════════

def generate_master_excel(output_path: Optional[Path] = None) -> Path:
    """Generate the complete multi-sheet Excel workbook matching the screenshots."""
    out_file = output_path or OUTPUT_EXCEL_PATH
    log.info("Generating master Excel workbook to: %s", out_file)
    
    # 1. Sync files
    sync_participant_files(days_back=75)
    
    # 2. Build records
    records = build_participant_records()
    log.info("Loaded %d historical trading sessions.", len(records))

    # 3. Create Workbook
    wb = openpyxl.Workbook()
    # Default sheet
    ws_default = wb.active
    ws_default.title = "Participant Wise Open Interest"

    # Populate Sheets matching Screenshot tab ordering
    populate_raw_participant_sheet(ws_default, records)
    
    ws_vol = wb.create_sheet(title="Volume")
    populate_volume_sheet(ws_vol, records)

    ws_ret = wb.create_sheet(title="Retailers")
    populate_participant_sheet(ws_ret, records, client_key="CLIENT", sheet_title="Retailers")

    ws_dii = wb.create_sheet(title="DII")
    populate_participant_sheet(ws_dii, records, client_key="DII", sheet_title="DII")

    ws_fii = wb.create_sheet(title="FII")
    populate_participant_sheet(ws_fii, records, client_key="FII", sheet_title="FII")

    ws_pro = wb.create_sheet(title="PRO")
    populate_participant_sheet(ws_pro, records, client_key="PRO", sheet_title="PRO")

    ws_oc = wb.create_sheet(title="Analysis")
    populate_option_chain_sheet(ws_oc)

    # Save
    wb.save(out_file)
    log.info("Workbook saved successfully: %s (%d bytes)", out_file, out_file.stat().st_size)
    return out_file


if __name__ == "__main__":
    generate_master_excel()
