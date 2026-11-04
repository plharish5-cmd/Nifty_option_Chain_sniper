#!/usr/bin/env python3
"""
pe_ce_bridge.py — 3-Tier StockMojo PE-CE Parameterization Bridge for Port 8500 Algo Engine
Tier 1 (Primary)   : Local Port 8600 App (http://127.0.0.1:8600/api/pe_ce_matrix)
Tier 2 (Secondary) : Dedicated Folder Watcher (~/Downloads/StockMojo_PE_CE/PE_CE_NIFTY_*.xls)
Tier 3 (Tertiary)  : Direct StockMojo URL / Session Cookie Poller
"""
import os
import glob
import json
import shutil
import urllib.request
import logging
import html.parser
import pandas as pd

log = logging.getLogger("pe_ce_bridge")

DOWNLOADS_ROOT = os.path.expanduser("~/Downloads")
PE_CE_FOLDER = os.path.expanduser("~/Downloads/StockMojo_PE_CE")
os.makedirs(PE_CE_FOLDER, exist_ok=True)

BRIDGE_CONFIG = {
    "primary_8600_url": "http://127.0.0.1:8600/api/pe_ce_matrix",
    "secondary_folder": PE_CE_FOLDER,
    "tertiary_sm_url": "",
    "tertiary_sm_cookie": "",
    "min_pcr_long": 0.90,
    "max_pcr_short": 1.10,
    "require_positive_pe_ce_day_for_long": True,
    "require_positive_pe_ce_3m_for_long": False
}


def sync_downloads_to_pe_ce_folder():
    """Auto-copies any newly downloaded PE_CE_NIFTY*.xls from ~/Downloads into ~/Downloads/StockMojo_PE_CE."""
    os.makedirs(PE_CE_FOLDER, exist_ok=True)
    for fpath in glob.glob(os.path.join(DOWNLOADS_ROOT, "PE_CE_NIFTY*.xls*")):
        dest = os.path.join(PE_CE_FOLDER, os.path.basename(fpath))
        if not os.path.exists(dest) or os.path.getmtime(fpath) > os.path.getmtime(dest):
            try:
                shutil.copy2(fpath, dest)
            except Exception as e:
                log.warning("Failed to copy %s to %s: %s", fpath, dest, e)


class _StockMojoHTMLTableParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.cur_row = []
        self.in_cell = False
        self.cell_data = ""

    def handle_starttag(self, tag, attrs):
        if tag in ("th", "td"):
            self.in_cell = True
            self.cell_data = ""
        elif tag == "tr":
            self.cur_row = []

    def handle_endtag(self, tag):
        if tag in ("th", "td"):
            self.in_cell = False
            self.cur_row.append(self.cell_data.strip())
        elif tag == "tr":
            if self.cur_row:
                self.rows.append(self.cur_row)

    def handle_data(self, data):
        if self.in_cell:
            self.cell_data += data


def read_stockmojo_table_file(filepath: str) -> list:
    """Reads HTML-based .xls table exported by StockMojo with zero-dependency fallback."""
    try:
        df = pd.read_html(filepath)[0]
        return df.to_dict(orient="records")
    except Exception as e_pd:
        log.debug("pandas read_html fallback to HTMLParser for %s: %s", filepath, e_pd)

    try:
        with open(filepath, "r", errors="ignore") as f:
            content = f.read()
        parser = _StockMojoHTMLTableParser()
        parser.feed(content)
        if len(parser.rows) >= 2:
            headers = parser.rows[0]
            records = [dict(zip(headers, r)) for r in parser.rows[1:] if len(r) == len(headers)]
            return records
    except Exception as e_parse:
        log.warning("Failed to parse %s: %s", filepath, e_parse)

    return []


def normalize_rows_and_verify_math(rows: list) -> list:
    """
    Ensures all 12 StockMojo columns exist and deterministically computes any missing derived columns.
    """
    normalized = []
    for i, r in enumerate(rows):
        put_day = int(float(r.get("Put OI Change (Day)", r.get("put_oi_day_chg", 0))))
        call_day = int(float(r.get("Call OI Change (Day)", r.get("call_oi_day_chg", 0))))

        # If 3m step isn't provided, compute from previous row (rows are ordered newest first)
        if "Put OI Change" in r and r["Put OI Change"] is not None and str(r["Put OI Change"]).strip() != "":
            put_3m = int(float(r["Put OI Change"]))
        elif "put_oi_step_chg" in r and r["put_oi_step_chg"] is not None:
            put_3m = int(float(r["put_oi_step_chg"]))
        elif i + 1 < len(rows):
            prev_row = rows[i + 1]
            prev_put_day = int(float(prev_row.get("Put OI Change (Day)", prev_row.get("put_oi_day_chg", put_day))))
            put_3m = put_day - prev_put_day
        else:
            put_3m = 0

        if "Call OI Change" in r and r["Call OI Change"] is not None and str(r["Call OI Change"]).strip() != "":
            call_3m = int(float(r["Call OI Change"]))
        elif "call_oi_step_chg" in r and r["call_oi_step_chg"] is not None:
            call_3m = int(float(r["call_oi_step_chg"]))
        elif i + 1 < len(rows):
            prev_row = rows[i + 1]
            prev_call_day = int(float(prev_row.get("Call OI Change (Day)", prev_row.get("call_oi_day_chg", call_day))))
            call_3m = call_day - prev_call_day
        else:
            call_3m = 0

        pe_ce_day = put_day - call_day
        pe_ce_3m = put_3m - call_3m
        pcr_val = round(float(r.get("PCR Total", r.get("pcr_day", 1.0))), 2)
        fut_ltp = round(float(r.get("Future LTP", r.get("fut_ltp", 23780.0))), 2)
        fut_oi_day = int(float(r.get("Future OI Change (Day)", r.get("fut_oi_day_chg", 0))))

        normalized.append({
            "Time": str(r.get("Time", r.get("time_label", "--:--"))),
            "Put OI Change (Day)": put_day,
            "Put OI Change": put_3m,
            "Call OI Change (Day)": call_day,
            "Call OI Change": call_3m,
            "PE-CE OI Change (Day)": pe_ce_day,
            "PE-CE OI Change": pe_ce_3m,
            "PE-CE Bar Change (Day)": pe_ce_day,
            "PE-CE Bar Change": pe_ce_3m,
            "PCR Total": pcr_val,
            "Future LTP": fut_ltp,
            "Future OI Change (Day)": fut_oi_day
        })
    return normalized


def fetch_pe_ce_3tier_hierarchy() -> dict:
    """
    Executes the 3-Tier Hierarchy:
      1. Primary   : Local Port 8600 App (http://127.0.0.1:8600/api/pe_ce_matrix)
      2. Secondary : ~/Downloads/StockMojo_PE_CE/PE_CE_NIFTY_*.xls
      3. Tertiary  : StockMojo URL Session Poller
    """
    # --- TIER 1: Primary Local Port 8600 App ---
    try:
        req = urllib.request.Request(BRIDGE_CONFIG["primary_8600_url"], method="GET")
        with urllib.request.urlopen(req, timeout=1.2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            rows = data if isinstance(data, list) else data.get("rows", [])
            if rows:
                clean_rows = normalize_rows_and_verify_math(rows)
                return {
                    "active_source": "TIER_1_PORT_8600_PRIMARY",
                    "source_detail": BRIDGE_CONFIG["primary_8600_url"],
                    "latest": clean_rows[0],
                    "rows": clean_rows[:40]
                }
    except Exception as e_t1:
        log.debug("Tier 1 Port 8600 unavailable: %s", e_t1)

    # --- TIER 2: Secondary StockMojo Downloads Folder (~/Downloads/StockMojo_PE_CE) ---
    try:
        sync_downloads_to_pe_ce_folder()
        search_paths = (
            glob.glob(os.path.join(PE_CE_FOLDER, "PE_CE_NIFTY*.xls*")) +
            glob.glob(os.path.join(DOWNLOADS_ROOT, "PE_CE_NIFTY*.xls*")) +
            glob.glob(os.path.join(os.getcwd(), "PE_CE_NIFTY*.xls*"))
        )
        files = sorted(set(search_paths), key=os.path.getmtime, reverse=True)
        if files:
            latest_file = files[0]
            rows = read_stockmojo_table_file(latest_file)
            if rows:
                clean_rows = normalize_rows_and_verify_math(rows)
                return {
                    "active_source": "TIER_2_DOWNLOADS_FOLDER",
                    "source_detail": latest_file,
                    "latest": clean_rows[0],
                    "rows": clean_rows[:40]
                }
    except Exception as e_t2:
        log.warning("Tier 2 Downloads Folder parsing error: %s", e_t2)

    # --- TIER 3: Tertiary StockMojo URL Session Poller ---
    if BRIDGE_CONFIG.get("tertiary_sm_url"):
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64)",
                "Accept": "application/json, text/plain, */*",
                "Cookie": BRIDGE_CONFIG.get("tertiary_sm_cookie", "")
            }
            req = urllib.request.Request(BRIDGE_CONFIG["tertiary_sm_url"], headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
                rows = raw if isinstance(raw, list) else raw.get("data", [])
                if rows:
                    clean_rows = normalize_rows_and_verify_math(rows)
                    return {
                        "active_source": "TIER_3_STOCKMOJO_SESSION_POLLER",
                        "source_detail": BRIDGE_CONFIG["tertiary_sm_url"],
                        "latest": clean_rows[0],
                        "rows": clean_rows[:40]
                    }
        except Exception as e_t3:
            log.warning("Tier 3 StockMojo Session Poller error: %s", e_t3)

    return {"active_source": "OFFLINE", "source_detail": "No active feed", "latest": {}, "rows": []}


def evaluate_pe_ce_parameter_gate(direction: str) -> dict:
    """
    Evaluates whether a LONG or SHORT paper/live trade passes the PE-CE & PCR parameterization rules.
    """
    feed = fetch_pe_ce_3tier_hierarchy()
    latest = feed.get("latest", {})
    if not latest:
        return {"passed": True, "source": "OFFLINE", "summary": "PE-CE feed offline (defaulting to pass)"}

    pcr = float(latest.get("PCR Total", 1.0))
    pe_ce_day = int(latest.get("PE-CE OI Change (Day)", 0))
    pe_ce_3m = int(latest.get("PE-CE OI Change", 0))
    put_day = int(latest.get("Put OI Change (Day)", 0))
    call_day = int(latest.get("Call OI Change (Day)", 0))

    reasons = []
    if direction.upper() == "LONG":
        if pcr < BRIDGE_CONFIG["min_pcr_long"]:
            reasons.append(f"PCR {pcr} < {BRIDGE_CONFIG['min_pcr_long']}")
        if BRIDGE_CONFIG["require_positive_pe_ce_day_for_long"] and pe_ce_day <= 0:
            reasons.append(f"PE-CE Day ({pe_ce_day}) <= 0")
        if BRIDGE_CONFIG["require_positive_pe_ce_3m_for_long"] and pe_ce_3m < 0:
            reasons.append(f"PE-CE 3m ({pe_ce_3m}) < 0")
    else:
        if pcr > BRIDGE_CONFIG["max_pcr_short"]:
            reasons.append(f"PCR {pcr} > {BRIDGE_CONFIG['max_pcr_short']}")
        if BRIDGE_CONFIG["require_positive_pe_ce_day_for_long"] and pe_ce_day >= 0:
            reasons.append(f"PE-CE Day (+{pe_ce_day}) Bullish")
        if BRIDGE_CONFIG["require_positive_pe_ce_3m_for_long"] and pe_ce_3m > 0:
            reasons.append(f"PE-CE 3m (+{pe_ce_3m}) Bullish")

    passed = len(reasons) == 0
    return {
        "passed": passed,
        "source": feed["active_source"],
        "source_detail": feed["source_detail"],
        "time": latest.get("Time"),
        "pcr": pcr,
        "pe_ce_day": pe_ce_day,
        "pe_ce_3m": pe_ce_3m,
        "put_day": put_day,
        "call_day": call_day,
        "blocked_reasons": reasons
    }
