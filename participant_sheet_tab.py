"""
participant_sheet_tab.py — the trainer's participant workbook inside the app.

  .venv/bin/streamlit run participant_sheet_tab.py        (standalone)
  or from dashboard.py:  import participant_sheet_tab; participant_sheet_tab.render()

Shows: FII/DII cash columns (BE, BD, DII AB) in his buy-green / sell-red, the Nifty & Bank
split, the FPI fortnight sector sheet (fpi_sector_tab), download of the updated workbook (.xlsm with his colours, formulas, charts and
Ctrl-key macros), today's three ratios, the last 20 sessions painted with his
conditional-formatting colours, his data bars for Net Options / Net Stock Futures,
the ratio-vs-Nifty chart, and the edge table re-computed from the sheet's own history.
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import participant_ratios as pr

HERE = Path(__file__).resolve().parent
LOG = pr.WORKBOOK.parent / "update.log"
COLS = {
    "nifty": "Nifty (AZ)",
    "nifty_chg_pct": "Chg %",
    "fii_AM": "FII PS/CS (AM)",
    "retail_AI": "Retail PS/CS (AI)",
    "pro_AB": "PRO PS/CS (AB)",
    "fii_Y_net_options": "FII Net Options (Y)",
    "fii_Z_net_futures": "FII Net Futures (Z)",
    "fii_AE_net_stock_fut": "FII Net Stock Fut (AE)",
    "fii_cash_prov": "FII cash NSE (BE)",
    "fii_cash_final": "FII cash NSDL (BD)",
    "dii_cash": "DII cash (AB)",
    "regime": "Regime",
}
CASH = ("fii_cash_prov", "fii_cash_final", "dii_cash")


@st.cache_data(ttl=300)
def _load(mtime: float) -> pd.DataFrame:
    return pr.load(pr.WORKBOOK)


def _style_table(df: pd.DataFrame):
    view = df[list(COLS)].rename(columns=COLS)
    view.index = pd.to_datetime(view.index).strftime("%d-%b-%Y %a")   # 25-Sep-2026 Fri, no 00:00:00
    view.index.name = "Date"
    rev = {v: k for k, v in COLS.items()}

    def paint(col: pd.Series):
        key = rev[col.name]
        out = []
        for v in col:
            fill, text = pr.cell_style(key, v) if key in ("fii_AM", "retail_AI", "pro_AB") + CASH else (None, None)
            out.append(f"background-color:{fill};color:{text}" if fill else "")
        return out

    sty = view.style.apply(paint, axis=0)
    # his data bars: green for Y / Z / AE (and red side for negatives)
    for key in ("fii_Y_net_options", "fii_Z_net_futures", "fii_AE_net_stock_fut"):
        sty = sty.bar(subset=[COLS[key]], align="zero", color=[pr.BAR_RED, pr.BAR_GREEN])
    sty = sty.format({COLS["nifty"]: "{:,.0f}", COLS["nifty_chg_pct"]: "{:+.2f}",
                      COLS["fii_AM"]: "{:.2f}", COLS["retail_AI"]: "{:.2f}", COLS["pro_AB"]: "{:.2f}",
                      COLS["fii_Y_net_options"]: "{:,.0f}", COLS["fii_Z_net_futures"]: "{:,.0f}",
                      COLS["fii_AE_net_stock_fut"]: "{:,.0f}", COLS["fii_cash_prov"]: "{:,.0f}",
                      COLS["fii_cash_final"]: "{:,.0f}", COLS["dii_cash"]: "{:,.0f}"}, na_rep="–")
    sty = sty.map(lambda v: f"color:{pr.GOOD_TEXT}" if isinstance(v, (int, float)) and v > 0 else
                  (f"color:{pr.BAD_TEXT}" if isinstance(v, (int, float)) and v < 0 else ""),
                  subset=[COLS["nifty_chg_pct"]])
    return sty


def _ratio_chart(df: pd.DataFrame, sessions: int) -> go.Figure:
    d = df.tail(sessions)
    fig = go.Figure()
    for key, name, colour in (("fii_AM", "FII (AM)", "#1f4e79"), ("retail_AI", "Retail (AI)", "#c55a11"),
                              ("pro_AB", "PRO (AB)", "#548235")):
        fig.add_trace(go.Scatter(x=d.index, y=d[key], name=name, mode="lines", line=dict(color=colour, width=2)))
    fig.add_hline(y=1.0, line_dash="dash", line_color="#7f7f7f", annotation_text="1.0 bullish / bearish line")
    fig.add_trace(go.Scatter(x=d.index, y=d["nifty"], name="Nifty", yaxis="y2", mode="lines",
                             line=dict(color="#a6a6a6", width=1.5)))
    fig.update_layout(height=420, margin=dict(l=10, r=10, t=30, b=10), legend=dict(orientation="h", y=1.08),
                      yaxis=dict(title="Put Short ÷ Call Short"),
                      yaxis2=dict(title="Nifty", overlaying="y", side="right", showgrid=False))
    return fig


def _net_bars(df: pd.DataFrame, sessions: int) -> go.Figure:
    d = df.tail(sessions)
    fig = go.Figure()
    for key, name in (("fii_Y_net_options", "FII Net Options (Y)"), ("fii_AE_net_stock_fut", "FII Net Stock Futures (AE)")):
        fig.add_trace(go.Bar(x=d.index, y=d[key], name=name,
                             marker_color=[pr.BAR_GREEN if v >= 0 else pr.BAR_RED for v in d[key]]))
    fig.update_layout(height=320, barmode="group", margin=dict(l=10, r=10, t=30, b=10),
                      legend=dict(orientation="h", y=1.1), yaxis=dict(title="contracts"))
    return fig


@st.cache_data(ttl=300)
def _nifty_bank(mtime: float) -> pd.DataFrame:
    """His 'Nifty & Bank' sheet (Ctrl+N / Ctrl+B), last 20 filled rows."""
    cols = {0: "Date", 1: "Nifty fut net", 2: "Fut avg buy", 3: "Fut avg sell", 5: "Nifty opt net",
            6: "Opt avg buy strike", 7: "Opt avg sell strike", 8: "Nifty opt net ₹Cr",
            13: "BN fut net", 17: "BN opt net", 20: "BN opt net ₹Cr"}
    df = pd.read_excel(pr.WORKBOOK, sheet_name="Nifty & Bank", header=None, skiprows=4,
                       usecols=list(cols), engine="openpyxl")
    df.columns = [cols[c] for c in sorted(cols)]
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    for c in df.columns[1:]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["Date", "Nifty fut net"]).tail(20).iloc[::-1]


def render() -> None:
    st.subheader("Participant sheet (Stock Unlimited method)")
    wb = pr.WORKBOOK
    if not wb.exists():
        st.warning(f"Workbook not found at {wb}. Copy Partcipant_Data_Sheet_NEW.xlsm there and run "
                   "`python update_participant_sheet.py`.")
        return
    df = _load(wb.stat().st_mtime)
    last = df.iloc[-1]

    c1, c2, c3, c4, c5 = st.columns([1, 1, 1, 1.3, 1.6])
    c1.metric("FII AM", f"{last.fii_AM:.2f}", f"{last.fii_AM - df.fii_AM.iloc[-2]:+.2f}")
    c2.metric("Retail AI", f"{last.retail_AI:.2f}", f"{last.retail_AI - df.retail_AI.iloc[-2]:+.2f}")
    c3.metric("PRO AB", f"{last.pro_AB:.2f}", f"{last.pro_AB - df.pro_AB.iloc[-2]:+.2f}")
    c4.metric("Regime", last.regime, f"as of {df.index[-1]:%d-%b-%Y}", delta_color="off")
    with c5:
        st.download_button("⬇ Download workbook (.xlsm)", wb.read_bytes(), file_name=wb.name,
                            mime="application/vnd.ms-excel.sheet.macroEnabled.12", use_container_width=True)
        if st.button("Update now from NSE", use_container_width=True):
            with st.spinner("Downloading NSE files and writing the workbook…"):
                res = subprocess.run([sys.executable, str(HERE / "update_participant_sheet.py")],
                                     capture_output=True, text=True, cwd=HERE, timeout=1200)
            st.code((res.stdout or "")[-2500:] + (res.stderr or "")[-1000:])
            st.cache_data.clear()

    st.caption(pr.summary_line(wb))
    st.markdown("**Last 20 sessions** — colours are the workbook's own conditional formatting "
                "(green > 1; FII AM < 0.55 green, > 1.3 red), bars are his data bars.")
    st.dataframe(_style_table(df.tail(20).iloc[::-1]), use_container_width=True, height=560)
    st.caption("FII cash NSDL (BD) for the latest day shows – until the next evening: NSDL publishes it one day "
               "late and the next run fills it (his rule, video 2 at 12:24).")

    sessions = st.select_slider("Chart window (sessions)", [60, 120, 250, 500, 1000], value=120)
    st.plotly_chart(_ratio_chart(df, sessions), use_container_width=True)
    st.plotly_chart(_net_bars(df, sessions), use_container_width=True)

    with st.expander("Does it work? Edge table from the sheet's own history"):
        st.dataframe(pr.edge_table(wb), use_container_width=True, hide_index=True)
        st.caption("Next-day / next-5-day Nifty direction after each reading, since Dec-2019.")
    st.divider()
    try:
        import participant_correlation_tab
        participant_correlation_tab.render()
    except Exception as exc:
        st.caption(f"Data-correlation section unavailable: {exc}")
    st.divider()

    with st.expander("Nifty & Bank split (Ctrl+N / Ctrl+B) — he found no strong correlation"):
        try:
            nbk = _nifty_bank(wb.stat().st_mtime)
            num_cols = [c for c in nbk.columns if c != "Date"]
            st.dataframe(nbk.style.format({c: "{:,.0f}" for c in num_cols}, na_rep="")
                         .map(lambda v: f"color:{pr.GOOD_TEXT}" if isinstance(v, (int, float)) and v > 0 else
                              (f"color:{pr.BAD_TEXT}" if isinstance(v, (int, float)) and v < 0 else ""),
                              subset=[c for c in num_cols if "net" in c]),
                         use_container_width=True, hide_index=True)
        except Exception as exc:
            st.caption(f"Nifty & Bank not readable: {exc}")
    if LOG.exists():
        with st.expander("Update log"):
            st.code("\n".join(LOG.read_text().splitlines()[-40:]))

    st.divider()
    try:
        import fpi_sector_tab
        fpi_sector_tab.render()
    except Exception as exc:
        st.caption(f"FPI sector section unavailable: {exc}")


if __name__ == "__main__":
    st.set_page_config(page_title="Participant sheet", layout="wide")
    render()
