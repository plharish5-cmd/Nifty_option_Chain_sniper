"""
Normalises an option chain into a single flat DataFrame, from either source:

  * the live NSE JSON API  -> parse_json()
  * the NSE website CSV    -> parse_csv()

Both produce identical columns, so everything downstream is source-agnostic.
The CSV path exists because it lets you test the whole pipeline offline
against a saved file, with no network and no market hours.

Canonical columns:
    strike
    ce_oi ce_chg_oi ce_vol ce_iv ce_ltp ce_chg ce_bid ce_ask
    pe_oi pe_chg_oi pe_vol pe_iv pe_ltp pe_chg pe_bid pe_ask
"""
from __future__ import annotations

import re
from datetime import datetime
import numpy as np
import pandas as pd

COLUMNS = [
    "strike", "expiry",
    "ce_oi", "ce_chg_oi", "ce_vol", "ce_iv", "ce_ltp", "ce_chg", "ce_bid", "ce_ask",
    "pe_oi", "pe_chg_oi", "pe_vol", "pe_iv", "pe_ltp", "pe_chg", "pe_bid", "pe_ask",
]


def _norm_exp(exp: str | None) -> str | None:
    """Normalize expiry date string to '%d-%b-%Y' (e.g. '15-Sep-2026')."""
    if not exp:
        return None
    s = str(exp).strip()
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%d-%b-%Y")
        except ValueError:
            pass
    return s


def _num(v) -> float:
    """NSE emits '-' for empty, commas inside quoted numbers, and blanks."""
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return np.nan
    s = str(v).strip().replace(",", "")
    if s in ("-", "", "nan", "None"):
        return np.nan
    try:
        return float(s)
    except ValueError:
        return np.nan


# ── live JSON ────────────────────────────────────────────────────────────────
def parse_json(payload: dict, expiry: str | None = None) -> pd.DataFrame:
    """
    Flatten the NSE API payload. Works for both the legacy endpoint (one blob
    containing every expiry, filtered here) and the v3 endpoint (already
    filtered server-side by the expiry parameter).

    If expiry='all', captures all expiries present without filtering.
    Always adds 'expiry' column to the returned DataFrame.
    """
    records = payload.get("records", payload)
    rows_in = records.get("data") or payload.get("filtered", {}).get("data") or []
    dates = records.get("expiryDates") or payload.get("expiryDates") or []

    filter_all = (expiry == "all")
    if expiry is None or expiry == "nearest":
        try:                                                    # EXPIRYROLL — first LIVE expiry
            from dhan_client import live_expiries
            target_expiry = (live_expiries(dates) or [None])[0] if dates else None
        except Exception:                                       # noqa: BLE001
            target_expiry = dates[0] if dates else None
    elif expiry == "all":
        target_expiry = None
    else:
        target_expiry = expiry

    norm_target = _norm_exp(target_expiry)

    def _rows(match_expiry: bool) -> list[dict]:
        out: list[dict] = []
        for item in rows_in:
            ce, pe = item.get("CE") or {}, item.get("PE") or {}
            raw_item_exp = item.get("expiryDate") or ce.get("expiryDate") or pe.get("expiryDate") or target_expiry
            norm_item_exp = _norm_exp(raw_item_exp)
            if match_expiry and norm_target and norm_item_exp != norm_target:
                continue
            out.append({
                "strike":     _num(item.get("strikePrice")),
                "expiry":     norm_item_exp or (str(raw_item_exp) if raw_item_exp else None),
                "ce_oi":      _num(ce.get("openInterest")),
                "ce_chg_oi":  _num(ce.get("changeinOpenInterest")),
                "ce_vol":     _num(ce.get("totalTradedVolume")),
                "ce_iv":      _num(ce.get("impliedVolatility")),
                "ce_ltp":     _num(ce.get("lastPrice")),
                "ce_chg":     _num(ce.get("change")),
                "ce_bid":     _num(ce.get("bidprice")),
                "ce_ask":     _num(ce.get("askPrice")),
                "pe_oi":      _num(pe.get("openInterest")),
                "pe_chg_oi":  _num(pe.get("changeinOpenInterest")),
                "pe_vol":     _num(pe.get("totalTradedVolume")),
                "pe_iv":      _num(pe.get("impliedVolatility")),
                "pe_ltp":     _num(pe.get("lastPrice")),
                "pe_chg":     _num(pe.get("change")),
                "pe_bid":     _num(pe.get("bidprice")),
                "pe_ask":     _num(pe.get("askPrice")),
            })
        return out

    rows = _rows(match_expiry=(not filter_all)) or _rows(match_expiry=False)
    if not rows:
        raise ValueError(
            f"no option rows in payload (keys={list(payload)[:6]}, "
            f"rows seen={len(rows_in)}) — run diagnose.py"
        )

    df = pd.DataFrame(rows, columns=COLUMNS)
    sort_cols = ["strike", "expiry"] if "expiry" in df.columns else ["strike"]
    return df.dropna(subset=["strike"]).sort_values(sort_cols).reset_index(drop=True)


# ── CSV export ───────────────────────────────────────────────────────────────
_CSV_ORDER = [
    "ce_oi", "ce_chg_oi", "ce_vol", "ce_iv", "ce_ltp", "ce_chg",
    "ce_bidqty", "ce_bid", "ce_ask", "ce_askqty",
    "strike",
    "pe_bidqty", "pe_bid", "pe_ask", "pe_askqty",
    "pe_chg", "pe_ltp", "pe_iv", "pe_vol", "pe_chg_oi", "pe_oi",
]


def parse_csv(path: str) -> pd.DataFrame:
    """
    Parse the CSV you get from the 'download' link on the option-chain page.

    Its quirks, all handled here: a two-row mirrored header (CALLS / STRIKE /
    PUTS on row 1, real names on row 2), a leading blank column, a trailing
    comma producing a phantom column, thousands separators inside quoted
    fields, and '-' for missing values.
    """
    raw = pd.read_csv(path, skiprows=1, header=0, dtype=str)
    raw = raw.iloc[:, 1:22]                       # drop leading blank + trailing phantom
    raw.columns = _CSV_ORDER

    for c in _CSV_ORDER:
        raw[c] = raw[c].map(_num)

    raw["expiry"] = None
    df = raw[COLUMNS].copy()
    return df.dropna(subset=["strike"]).sort_values("strike").reset_index(drop=True)


# ── spot inference ───────────────────────────────────────────────────────────
def infer_spot(df: pd.DataFrame) -> float:
    """
    Derive spot from the chain itself using put-call parity:
        forward = strike + CE - PE
    Averaged over the strikes where CE and PE premiums are closest together,
    which is where parity is least distorted by illiquidity.

    Only needed for the CSV path — the live API gives you underlyingValue
    directly, and you should always prefer that.
    """
    both = df.dropna(subset=["ce_ltp", "pe_ltp"]).copy()
    if both.empty:
        raise ValueError("cannot infer spot: no strike has both CE and PE prices")
    both["synth"] = both.strike + both.ce_ltp - both.pe_ltp
    both["gap"] = (both.ce_ltp - both.pe_ltp).abs()
    return float(both.nsmallest(8, "gap")["synth"].median())


def is_expired_chain(df: pd.DataFrame, spot: float, tol: float = 0.5) -> bool:
    """
    True when every option is trading at pure intrinsic value — which only
    happens at expiry. Such a chain has no forward-looking information: the
    OI in it is about to be extinguished. Worth flagging loudly rather than
    analysing as if it were live.
    """
    tv = []
    for _, r in df.iterrows():
        if not np.isnan(r.ce_ltp):
            tv.append(r.ce_ltp - max(0.0, spot - r.strike))
        if not np.isnan(r.pe_ltp):
            tv.append(r.pe_ltp - max(0.0, r.strike - spot))
    return bool(tv) and float(np.nanmedian(tv)) <= tol


# ── Aliases & Unified parse() entrypoint ─────────────────────────────────────
load_csv = parse_csv


def parse(raw, expiry: str | None = None) -> tuple[pd.DataFrame, dict]:
    """
    Unified entrypoint returning (df, meta).
    Handles JSON payload dict, CSV file path, or existing DataFrame.
    """
    from pathlib import Path
    if isinstance(raw, dict):
        df = parse_json(raw, expiry=expiry)
        try:
            spot = infer_spot(df)
        except Exception:
            records = raw.get("records", raw)
            spot = float(records.get("underlyingValue", 0.0) or 0.0)
        records = raw.get("records", raw) if isinstance(raw, dict) else {}
        ts = records.get("timestamp", "") if isinstance(records, dict) else ""
        meta = {"spot": spot, "timestamp": ts, "expiry": expiry}
        return df, meta
    elif isinstance(raw, (str, Path)):
        df = parse_csv(str(raw))
        try:
            spot = infer_spot(df)
        except Exception:
            spot = 0.0
        meta = {"spot": spot, "timestamp": "", "expiry": expiry}
        return df, meta
    elif isinstance(raw, pd.DataFrame):
        try:
            spot = infer_spot(raw)
        except Exception:
            spot = 0.0
        meta = {"spot": spot, "timestamp": "", "expiry": expiry}
        return raw, meta
    return pd.DataFrame(), {"spot": 0.0, "timestamp": "", "expiry": expiry}
