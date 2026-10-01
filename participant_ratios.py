"""
participant_ratios.py — the trainer's key numbers, computed from the workbook itself.

His three "most important" columns (video 28:29):
  FII!Y   Net Options Positions = Net CE - Net PE = (L-P) - (N-R)
  FII!AE  Net Stock Futures     = H - J
  FII!AM  Put Short / Call Short = R / P      (also Retailers!AI and PRO!AB)
His colour rules, copied from the workbook's conditional formatting:
  AM / AI / AB > 1            -> green (Excel "Good": fill #C6EFCE, text #006100)
  FII!AM < 0.55 (rows 1334+)  -> green  (his contrarian extreme)
  FII!AM > 1.3  (rows 1334+)  -> red    (Excel "Bad": fill #FFC7CE, text #9C0006)
  Y, Z, T, AE data bars green #63C384; V (Net PE) data bar red #FF555A
  Cash columns FII!BE (NSE provisional), FII!BD (NSDL final, one day late), DII!AB:
  buying green / selling red (video 2, 8:52)
"""
from __future__ import annotations

from pathlib import Path

import warnings

import pandas as pd

warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")

import config

GOOD_FILL, GOOD_TEXT = "#C6EFCE", "#006100"
BAD_FILL, BAD_TEXT = "#FFC7CE", "#9C0006"
BAR_GREEN, BAR_RED = "#63C384", "#FF555A"
WORKBOOK = config.DATA_DIR / "participant_sheet" / "Partcipant_Data_Sheet_NEW.xlsm"

# raw columns in the participant sheets (same letters on FII / Retailers / DII / PRO)
RAW = {"D": "fut_idx_long", "F": "fut_idx_short", "H": "fut_stk_long", "J": "fut_stk_short",
       "L": "call_long", "N": "put_long", "P": "call_short", "R": "put_short"}


def _col(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def _read_sheet(path: Path, sheet: str, extra: dict[str, str] | None = None) -> pd.DataFrame:
    cols = {"A": "date", **RAW, **(extra or {})}
    df = pd.read_excel(path, sheet_name=sheet, header=None, skiprows=1,
                       usecols=[_col(c) for c in cols], engine="openpyxl")
    df.columns = [cols[c] for c in sorted(cols, key=_col)]
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for c in df.columns:
        if c != "date":
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["date", "put_short", "call_short"])
    return df.drop_duplicates("date").set_index("date").sort_index()


def load(path: Path | str = WORKBOOK) -> pd.DataFrame:
    """One row per trading day with the trainer's columns, oldest first."""
    path = Path(path)
    fii = _read_sheet(path, "FII", {"AZ": "nifty", "BD": "fii_cash_final", "BE": "fii_cash_prov"})
    ret = _read_sheet(path, "Retailers")
    pro = _read_sheet(path, "PRO")
    dii = _read_sheet(path, "DII", {"AB": "dii_cash"})
    out = pd.DataFrame(index=fii.index)
    out["nifty"] = fii["nifty"]
    out["fii_cash_prov"] = fii["fii_cash_prov"]
    out["fii_cash_final"] = fii["fii_cash_final"]
    out["dii_cash"] = dii["dii_cash"].reindex(out.index)
    out["nifty_chg_pct"] = fii["nifty"].pct_change() * 100
    out["fii_Y_net_options"] = (fii.call_long - fii.call_short) - (fii.put_long - fii.put_short)
    out["fii_Z_net_futures"] = fii.fut_idx_long - fii.fut_idx_short
    out["fii_AE_net_stock_fut"] = fii.fut_stk_long - fii.fut_stk_short
    out["fii_AM"] = fii.put_short / fii.call_short
    out["retail_AI"] = ret.put_short / ret.call_short
    out["pro_AB"] = pro.put_short / pro.call_short
    out["retail_V_net_options"] = (ret.call_long - ret.call_short) - (ret.put_long - ret.put_short)
    out["pro_V_net_options"] = (pro.call_long - pro.call_short) - (pro.put_long - pro.put_short)
    ratios = out[["fii_AM", "retail_AI", "pro_AB"]]
    out["regime"] = "MIXED"
    out.loc[(ratios > 1).all(axis=1), "regime"] = "ALL ABOVE 1"
    out.loc[(ratios <= 1).all(axis=1), "regime"] = "ALL BELOW 1"
    return out


def cell_style(col: str, value: float) -> tuple[str | None, str | None]:
    """(fill, text colour) exactly as his conditional formatting would paint the cell."""
    if pd.isna(value):
        return None, None
    if col == "fii_AM":
        if value > 1.3:
            return BAD_FILL, BAD_TEXT
        if value > 1 or value < 0.55:
            return GOOD_FILL, GOOD_TEXT
        return None, None
    if col in ("retail_AI", "pro_AB") and value > 1:
        return GOOD_FILL, GOOD_TEXT
    if col in ("fii_cash_prov", "fii_cash_final", "dii_cash") and value != 0:
        return (GOOD_FILL, GOOD_TEXT) if value > 0 else (BAD_FILL, BAD_TEXT)
    return None, None


def summary_line(path: Path | str = WORKBOOK) -> str:
    df = load(path)
    last = df.iloc[-1]
    below = (df["fii_AM"] < 1)[::-1]
    streak = int(below.cumprod().sum()) if bool(below.iloc[0]) else 0
    tail = f" (FII below 1 for {streak} sessions)" if streak >= 2 else ""
    return (f"Participants {df.index[-1]:%d-%b}: FII {last.fii_AM:.2f} | Retail {last.retail_AI:.2f} | "
            f"PRO {last.pro_AB:.2f} -> {last.regime}{tail}")


def edge_table(path: Path | str = WORKBOOK) -> pd.DataFrame:
    """Next-day / next-5-day behaviour for each reading, so the edge is re-checked as data grows."""
    df = load(path)
    r1 = df["nifty"].shift(-1) / df["nifty"] - 1
    r5 = df["nifty"].shift(-5) / df["nifty"] - 1
    rows = []
    groups = {
        "All three > 1": df.regime == "ALL ABOVE 1",
        "FII AM > 1": df.fii_AM > 1,
        "Retail AI > 1": df.retail_AI > 1,
        "PRO AB > 1": df.pro_AB > 1,
        "Mixed": df.regime == "MIXED",
        "FII AM <= 1": df.fii_AM <= 1,
        "All three <= 1": df.regime == "ALL BELOW 1",
        "FII AM < 0.55 (his green)": df.fii_AM < 0.55,
        "FII AM > 1.3 (his red)": df.fii_AM > 1.3,
        "FII cash buying (NSE BE > 0)": df.fii_cash_prov > 0,
        "FII cash selling (NSE BE < 0)": df.fii_cash_prov < 0,
        "FII final buying (NSDL BD > 0, known next day)": df.fii_cash_final > 0,
        "FII final selling (NSDL BD < 0, known next day)": df.fii_cash_final < 0,
        "DII cash buying (AB > 0)": df.dii_cash > 0,
        "DII cash selling (AB < 0)": df.dii_cash < 0,
        "Any day": pd.Series(True, index=df.index),
    }
    for name, m in groups.items():
        a1, a5 = (r1.shift(-1), r5.shift(-1)) if "known next day" in name else (r1, r5)  # from when it is known
        m = m & a1.notna()
        rows.append({"Reading": name, "Days": int(m.sum()),
                     "Next day up %": round(100 * (a1[m] > 0).mean(), 1),
                     "Next 5 days up %": round(100 * (a5[m] > 0).mean(), 1),
                     "Avg 5-day move %": round(100 * a5[m].mean(), 2)})
    return pd.DataFrame(rows)


if __name__ == "__main__":
    print(summary_line())
    print(edge_table().to_string(index=False))
