"""
fpi_sector_tab.py — the trainer's 'FPI Fortnight Data' Sheet2 inside the app.

  .venv/bin/streamlit run fpi_sector_tab.py        (standalone)
  or: import fpi_sector_tab; fpi_sector_tab.render()   (participant_sheet_tab calls it)

Shows his Sheet2 (sectors x fortnights, ₹ Cr equity net investment) with his colours
(green > 0, red < 0, Sovereign / Others / Grand Total uncoloured as in his sheet),
the latest fortnight as bars, a download of the updated workbook, an update button,
and a history check of what the numbers did and did not tell us.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import participant_ratios as pr
import update_fpi_fortnight as uf

HERE = Path(__file__).resolve().parent
UNCOLOURED = {"Sovereign", "Others", "Grand Total"}          # outside his D4:XFD25 rule


@st.cache_data(ttl=600)
def _hist(mtime: float) -> pd.DataFrame:
    return uf.sheet_history(uf.WORKBOOK)


def _pivot(h: pd.DataFrame, n: int) -> pd.DataFrame:
    ends = sorted(h["end"].unique())[-n:]
    p = h[h["end"].isin(ends)].pivot_table(index="sector", columns="end", values="net_cr", aggfunc="first")
    order = list(dict.fromkeys(h["sector"]))                   # his row order
    p = p.reindex([s for s in order if s in p.index])
    p.columns = [uf.nsdl_fpi.fortnight_label(e) + f" {e:%y}" for e in p.columns]
    return p


def _style(p: pd.DataFrame):
    def paint(row):
        if row.name in UNCOLOURED:
            return ["font-weight:bold" if row.name == "Grand Total" else ""] * len(row)
        return [f"background-color:{pr.GOOD_FILL};color:{pr.GOOD_TEXT}" if v > 0 else
                f"background-color:{pr.BAD_FILL};color:{pr.BAD_TEXT}" if v < 0 else "" for v in row]
    return p.style.apply(paint, axis=1).format("{:,.0f}", na_rep="")


def _latest_bars(h: pd.DataFrame) -> go.Figure:
    last = h["end"].max()
    d = h[(h["end"] == last) & (~h["sector"].isin(UNCOLOURED))].sort_values("net_cr")
    fig = go.Figure(go.Bar(x=d["net_cr"], y=d["sector"], orientation="h",
                           marker_color=[pr.BAR_GREEN if v >= 0 else pr.BAR_RED for v in d["net_cr"]]))
    fig.update_layout(height=620, margin=dict(l=10, r=10, t=30, b=10),
                      title=f"FPI equity net, {uf.nsdl_fpi.fortnight_label(last)} {last:%Y} (₹ Cr)")
    return fig


def history_check(h: pd.DataFrame) -> pd.DataFrame:
    """Grand Total vs Nifty (from the participant sheet's FII!AZ), and sector persistence."""
    try:
        nifty = pr.load(pr.WORKBOOK)["nifty"].dropna()
    except Exception:
        return pd.DataFrame()
    nifty.index = pd.to_datetime(nifty.index)

    def px(t):
        x = nifty[nifty.index <= t]
        return x.iloc[-1] if len(x) else np.nan
    g = h[h["sector"] == "Grand Total"].copy()
    rows = []
    for _, r in g.iterrows():
        e = pd.Timestamp(r["end"])
        start = (e.replace(day=1) - pd.Timedelta(days=1)) if e.day == 15 else e.replace(day=15)
        pub = e + pd.Timedelta(days=4)                       # report is out 2-4 days later
        rows.append({"net": r["net_cr"], "same": px(e) / px(start) - 1,
                     "next": px(pub + pd.Timedelta(days=15)) / px(pub) - 1})
    t = pd.DataFrame(rows).dropna()
    p = h[~h["sector"].isin(UNCOLOURED)].pivot_table(index="end", columns="sector", values="net_cr").sort_index()
    persist = (np.sign(p) == np.sign(p.shift(-1))).where(p.notna() & p.shift(-1).notna()).stack().mean()
    return pd.DataFrame([
        {"Check": "Grand Total vs Nifty in the SAME fortnight (rank corr.)",
         "Value": round(t[["net", "same"]].corr("spearman").iloc[0, 1], 2), "Fortnights": len(t)},
        {"Check": "Grand Total vs Nifty in the NEXT fortnight, from publish date (rank corr.)",
         "Value": round(t[["net", "next"]].corr("spearman").iloc[0, 1], 2), "Fortnights": len(t)},
        {"Check": "Nifty up next fortnight after net BUYING (%)",
         "Value": round(100 * (t[t.net > 0]["next"] > 0).mean(), 1), "Fortnights": int((t.net > 0).sum())},
        {"Check": "Nifty up next fortnight after net SELLING (%)",
         "Value": round(100 * (t[t.net < 0]["next"] > 0).mean(), 1), "Fortnights": int((t.net < 0).sum())},
        {"Check": "Sector keeps the same buy/sell direction next fortnight (%)",
         "Value": round(100 * float(persist), 1), "Fortnights": int(p.shape[0])},
    ])


def render() -> None:
    st.subheader("FPI sector flows (fortnightly, NSDL) — his 'FPI Fortnight Data' Sheet2")
    wb = uf.WORKBOOK
    if not wb.exists():
        st.info(f"Copy 'FPI Fortnight Data.xlsx' to {wb} and run `python update_fpi_fortnight.py`.")
        return
    h = _hist(wb.stat().st_mtime)
    if h.empty:
        st.warning("Sheet2 has no readable fortnights.")
        return
    c1, c2 = st.columns([3, 1.4])
    with c2:
        st.download_button("⬇ Download FPI workbook (.xlsx)", wb.read_bytes(), file_name=wb.name,
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           use_container_width=True)
        if st.button("Check NSDL for a new fortnight", use_container_width=True):
            with st.spinner("Fetching the NSDL fortnightly report…"):
                res = subprocess.run([sys.executable, str(HERE / "update_fpi_fortnight.py")],
                                     capture_output=True, text=True, cwd=HERE, timeout=300)
            st.code((res.stdout or "")[-2000:] + (res.stderr or "")[-800:])
            st.cache_data.clear()
        last = h["end"].max()
        gt = h[(h["end"] == last) & (h["sector"] == "Grand Total")]["net_cr"]
        if len(gt):
            st.metric(f"Grand Total {uf.nsdl_fpi.fortnight_label(last)}", f"₹{gt.iloc[0]:,.0f} Cr")
        st.caption("New report: 2–4 days after the 15th and month end. The timer checks daily.")
    with c1:
        n = st.select_slider("Fortnights shown", [6, 12, 24, 48], value=12, key="fpi_n")
        st.dataframe(_style(_pivot(h, n)), use_container_width=True, height=900)
    st.plotly_chart(_latest_bars(h), use_container_width=True)
    with st.expander("Does it work? History check"):
        st.dataframe(history_check(h), use_container_width=True, hide_index=True)
        st.caption("He uses this sheet to see WHICH sectors FPIs buy or sell, not as a Nifty timing signal.")


if __name__ == "__main__":
    st.set_page_config(page_title="FPI sectors", layout="wide")
    render()
