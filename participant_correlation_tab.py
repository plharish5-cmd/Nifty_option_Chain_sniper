"""
participant_correlation_tab.py — Lecture 2 "Data Correlation" inside the app.

  .venv/bin/streamlit run participant_correlation_tab.py      (standalone)
  participant_sheet_tab.render() calls render() below the last-20-sessions table.

Today's reading of every rule he gave (cards), his portfolio chart (Nifty with FII stock
futures and his 2.2L / 1.25L / 1L lines), the net-options (Y) chart with his -80k line,
the history check of every rule, and his trading notes from the lecture.
"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

import participant_correlation as pc
import participant_ratios as pr

STATE_STYLE = {
    "BULL": (pr.GOOD_FILL, pr.GOOD_TEXT, "▲"),
    "BEAR": (pr.BAD_FILL, pr.BAD_TEXT, "▼"),
    "NEUTRAL": ("#E7E6E6", "#3A3A3A", "●"),
    "INFO": ("#DDEBF7", "#1F4E79", "i"),
}

NOTES = [
    ("Which data", "For the portfolio look at FII only, not PRO: FII rallies come from stock futures (or cash equity). "
                   "Y, AE and AM are the three key columns; AM is ahead of Y. (1:29, 14:47-15:22)"),
    ("Y vs AM", "Y moves with lot-size changes (-80k at lot 50 became about -1.6L when lots halved). "
                "Prefer AM. (24:39-25:50)"),
    ("Sizing", "Invest 1/5 (or 1/6) of the amount meant for the portfolio per step. Add as AE keeps rising. Keep margin in hand. "
               "(4:12-4:20, 6:21-6:36, 41:22-41:39)"),
    ("Falling market", "AE rising above 2-2.5L while Nifty falls = they accumulate. Add, don't fear. COVID March 2020: "
                       "AE 4-6L while Nifty fell, the buying time. (18:30-18:44, 31:53-34:14)"),
    ("Exit", "Top forms when AE drops back to 1-1.5L. Book part near 1.2-1.25L, empty the portfolio below 1L. "
             "Expect to exit 200-300 pts below the exact top. (4:35-5:33, 8:04-8:57, 27:13-27:30)"),
    ("Bottoms", "Near a bottom Y is still below -80k but getting less negative (shorts covering), and AM crosses 1 first. "
                "The exact bottom comes from price action (double bottom, zones), not data. (16:16-16:47, 20:42-21:40)"),
    ("Monthly expiry", "After monthly expiry futures and monthly options roll off, so the data jumps (Y -1.26L to -23k). "
                       "Read the days after. (21:48-22:11)"),
    ("Targets", "No FII target is known. His targets: previous high or the next level above; "
                "Fibonacci extension levels later. Book at your target, then sit out if unclear. (27:36-29:12)"),
    ("Instruments", "Buy futures or write options. Option buying is last, only for masters. If the setup comes late in the "
                    "month (20th+) use next month's future; for a weekly setup buy next week's expiry. (39:11-40:15)"),
    ("Timeframe", "15-minute chart: fewer stop-loss hits, cleaner analysis, slightly later entries. (47:11-47:54)"),
    ("First 15 min", "Avoid entering in the first 15 minutes: VIX is high for 5-10 minutes, then premiums drop fast. "
                     "For a straddle, wait for the first 15-min candle. (57:39-58:24)"),
    ("Levels", "Work level to level. Give both an upper and a lower level each day: upper break -> call writers trapped -> up; "
               "lower break -> put writers exit -> long unwinding down. (1:07:24-1:08:49)"),
    ("Weekly candles", "Bullish engulfing on the weekly chart = long with SL below it; evening star = down. "
                       "Keep one simple setup. (37:10-38:42)"),
    ("Retail AI", "Retail AI > 1 matters because the few retail traders who write options mostly write puts (smart money in retail). "
                  "(42:40-43:02)"),
    ("Backtest", "His method: pick a past Monday, note what FII / PRO / Retail did, open the next day's 15-min chart, "
                 "write what happened, and check it repeats. Do it on weekends. (1:05:04-1:05:51)"),
    ("Keep private", "He asked that the lecture not be shared outside friends and family. (34:39-34:56)"),
]


@st.cache_data(ttl=600)
def _data(mtime: float):
    R = pc.rules()
    d = pc.add_signals(pc.load_dataset(pc.WORKBOOK), R, pc.load_ohlc(R))
    return d, R, pc.history_table(d, R), pc.portfolio_trades(d), pc.today_cards(d, R)


def _cards(cards: list[dict]) -> None:
    cols = st.columns(2)
    for i, c in enumerate(cards):
        fill, text, icon = STATE_STYLE.get(c["state"], STATE_STYLE["INFO"])
        cols[i % 2].markdown(
            f"<div style='border-left:6px solid {text};background:{fill};color:{text};padding:8px 12px;"
            f"border-radius:6px;margin-bottom:8px'>"
            f"<b>{icon} {c['rule']}</b> — <b>{c['state']}</b><br>"
            f"<span style='font-size:0.88em'>{c['reading']}</span><br>"
            f"<span style='font-size:0.88em;color:#222'>{c['action']}</span></div>",
            unsafe_allow_html=True)


def _portfolio_chart(d: pd.DataFrame, R: dict, n: int, scaled: bool) -> go.Figure:
    x = d.tail(n)
    P = R["portfolio_stock_futures"]
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Scatter(x=x.index, y=x["nifty"], name="Nifty", line=dict(color="#1F4E79", width=2)),
                  secondary_y=False)
    fig.add_trace(go.Bar(x=x.index, y=x["AE"], name="FII stock futures (AE)", opacity=0.45,
                         marker_color=[pr.BAR_GREEN if v >= 0 else pr.BAR_RED for v in x["AE"]]), secondary_y=True)
    sc = x["AE_scale"] if scaled else pd.Series(1.0, index=x.index)
    for key, name, colour in (("entry", "build 2.2L", "#006100"), ("partial_book", "book part 1.25L", "#C65911"),
                              ("exit", "exit 1L", "#9C0006")):
        fig.add_trace(go.Scatter(x=x.index, y=P[key] * sc, name=name + (" (scaled)" if scaled else ""),
                                 line=dict(color=colour, dash="dash", width=1.5)), secondary_y=True)
    expo = x["pf_expo_scaled" if scaled else "pf_expo"]
    fig.add_trace(go.Scatter(x=x.index, y=expo * 5, name="tranches held (of 5)", line=dict(color="#7030A0", width=1),
                             visible="legendonly"), secondary_y=True)
    fig.update_layout(height=430, margin=dict(l=10, r=10, t=30, b=10), legend=dict(orientation="h", y=1.1),
                      bargap=0)
    fig.update_yaxes(title_text="Nifty", secondary_y=False)
    fig.update_yaxes(title_text="contracts", secondary_y=True, showgrid=False)
    return fig


def _y_chart(d: pd.DataFrame, n: int) -> go.Figure:
    x = d.tail(n)
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Bar(x=x.index, y=x["Y"], name="FII net options (Y)", opacity=0.55,
                         marker_color=[pr.BAR_GREEN if a >= b else pr.BAR_RED for a, b in zip(x["Y"], x["Y_line_scaled"])]),
                  secondary_y=False)
    fig.add_trace(go.Scatter(x=x.index, y=x["Y_line"], name="his line -80k", line=dict(color="#9C0006", dash="dash")),
                  secondary_y=False)
    fig.add_trace(go.Scatter(x=x.index, y=x["Y_line_scaled"], name="scaled line (1-yr median)",
                             line=dict(color="#C65911", dash="dot")), secondary_y=False)
    fig.add_trace(go.Scatter(x=x.index, y=x["AM"], name="FII AM", line=dict(color="#1F4E79", width=1.5)),
                  secondary_y=True)
    fig.add_hline(y=0.97, line_dash="dot", line_color="#006100", secondary_y=True)
    fig.update_layout(height=380, margin=dict(l=10, r=10, t=30, b=10), legend=dict(orientation="h", y=1.12), bargap=0)
    fig.update_yaxes(title_text="Y contracts", secondary_y=False)
    fig.update_yaxes(title_text="AM", secondary_y=True, showgrid=False)
    return fig


def render() -> None:
    st.subheader("Data correlation (Lecture 2) — what his sheet says for the next session")
    if not pc.WORKBOOK.exists():
        st.info("Participant workbook not found.")
        return
    d, R, hist, trades, cards = _data(pc.WORKBOOK.stat().st_mtime)
    st.caption(f"Readings after the {d.index[-1]:%d-%b-%Y} close. Green = bullish, red = bearish, blue = setup checks.")
    _cards(cards)

    c1, c2 = st.columns([3, 1])
    with c2:
        n = st.select_slider("Window (sessions)", [60, 120, 250, 500, 1000, 1700], value=250, key="corr_n")
        scaled = st.toggle("Scale his AE lines to today's levels", value=False, key="corr_scaled",
                           help="His 2.2L/1.25L/1L date from 2021-22; FII stock-futures OI is ~5-10x larger since 2024.")
    with c1:
        st.plotly_chart(_portfolio_chart(d, R, n, scaled), use_container_width=True)
    st.plotly_chart(_y_chart(d, n), use_container_width=True)

    with st.expander("Does it work? History check of every rule (2019-12 onward)", expanded=True):
        st.dataframe(hist, use_container_width=True, hide_index=True)
        st.caption("Hit % = share of signal days with the outcome; All days % = same outcome on every comparable day. "
                   "Fewer than 10 days = INSUFFICIENT SAMPLE. R1 rows compare Nifty's average daily move while the "
                   "portfolio is held vs out. R6-R9 use Nifty daily OHLC (Dhan) and adapt his Thursday-expiry "
                   "weekdays to the expiry calendar (Tuesday since Sep-2025).")
    with st.expander("Portfolio entries and exits by his stock-futures rule"):
        st.dataframe(trades, use_container_width=True, hide_index=True)
    with st.expander("His trading notes from this lecture"):
        for title, text in NOTES:
            st.markdown(f"**{title}.** {text}")


if __name__ == "__main__":
    st.set_page_config(page_title="Data correlation", layout="wide")
    render()
