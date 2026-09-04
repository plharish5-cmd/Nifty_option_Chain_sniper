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
import numpy as np
import pandas as pd

COLUMNS = [
    "strike",
    "ce_oi", "ce_chg_oi", "ce_vol", "ce_iv", "ce_ltp", "ce_chg", "ce_bid", "ce_ask",
    "pe_oi", "pe_chg_oi", "pe_vol", "pe_iv", "pe_ltp", "pe_chg", "pe_bid", "pe_ask",
]


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

    If filtering by expiry yields nothing, we assume the payload was already
    filtered and use every row — otherwise a v3 response whose rows carry no
    expiryDate field would silently produce an empty chain.
    """
    records = payload.get("records", payload)
    rows_in = records.get("data") or payload.get("filtered", {}).get("data") or []
    if expiry is None:
        dates = records.get("expiryDates") or payload.get("expiryDates") or []
        expiry = dates[0] if dates else None

    def _rows(match_expiry: bool) -> list[dict]:
        out: list[dict] = []
        for item in rows_in:
            if match_expiry and expiry and item.get("expiryDate") != expiry:
                continue
            ce, pe = item.get("CE") or {}, item.get("PE") or {}
            out.append({
                "strike":     _num(item.get("strikePrice")),
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

    rows = _rows(match_expiry=True) or _rows(match_expiry=False)
    if not rows:
        raise ValueError(
            f"no option rows in payload (keys={list(payload)[:6]}, "
            f"rows seen={len(rows_in)}) — run diagnose.py"
        )

    df = pd.DataFrame(rows, columns=COLUMNS)
    return df.dropna(subset=["strike"]).sort_values("strike").reset_index(drop=True)


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
