"""
mentor_option_chain_tab.py — the trainer's option-chain method (Lecture 3) as a page.

  .venv/bin/streamlit run mentor_option_chain_tab.py
  or inside dashboard.py later:  import mentor_option_chain_tab; mentor_option_chain_tab.render()

Reads the snapshots your scanner stores; shows his top rows (totals, PCRs, writer side, volume
side), his ladder (100-pt strikes, top-2 max OI marked red, ITM shaded), S/R zones from strike +
premium, ITM traps, buildups, his through-the-day graphs, the 15-min candle structure with
inside candles in black, a history check and his notes.
"""
from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

import mentor_option_chain as m

GOOD_FILL, GOOD_TEXT = "#C6EFCE", "#006100"
BAD_FILL, BAD_TEXT = "#FFC7CE", "#9C0006"
BAR_GREEN, BAR_RED = "#63C384", "#FF555A"

NOTES = [
    ("Writer's view", "Read the chain from the WRITER's side: writers put in more money to earn less, so they are "
                      "more careful than a buyer of a Rs 15 option (8:08-9:35)."),
    ("Change in OI", "Always read change in OI for the day; total OI carries old positions (14:12-14:24)."),
    ("Not alone", "Aggressive call writers + your price-action setup = short. The chain alone is not a trade; it flips "
                  "fast when short covering starts. Works about 8 of 10 times (12:48-14:12)."),
    ("Momentum", "Big candles come when IN-THE-MONEY writers get trapped: ITM put writers falling behind -> SL -> "
                 "they buy puts -> next strike's SL -> long unwinding chain. ITM call writers -> short covering. "
                 "If retail holds the ITM call writing and FII/PRO are bullish, expect the market to go up to trap them "
                 "(15:10-19:39, 26:28-28:01)."),
    ("Watch 1-2 strikes ITM", "Look 100-200 pts in the money: writers adding there = confident; closing = the move "
                              "against them has begun (28:06-29:47)."),
    ("S/R from the chain", "Resistance = max call-OI strike + its premium; support = max put-OI strike - its premium. "
                           "Beyond the zone writers exit -> momentum. Chart-based S/R (next lectures) is better "
                           "(42:09-44:46)."),
    ("Near ATM only", "Max OI near ATM is authentic. Far OTM = hedges; ignore beyond 200-300 pts (46:50-48:05)."),
    ("Volume rule", "More volume on one side -> market tends to go that way. He has NOT fully backtested it: test 1-2 "
                    "weeks first, and it may not work on expiry day (35:29-41:55). See the history check below."),
    ("How he trades", "One direction only: expects down -> writes calls at the broken level; expects up -> writes puts. "
                      "No short straddles/strangles. Option buying only slightly ITM in rare momentum cases. "
                      "Writing wins 2 of 3 outcomes (up / sideways / down); buying 1 of 3 (19:43-22:08)."),
    ("Before any trade", "Know entry, stop-loss and target, or don't enter. His targets: Nifty 50-70 pts, "
                         "BankNifty 200-250 pts. No 5-10 pt scalps (22:47-23:43)."),
    ("Futures chart", "Draw levels, SL and targets on the FUTURES chart; option premiums come from futures. Spot only "
                      "picks ITM/ATM/OTM. Near monthly expiry the next future trades ~150-200 pts higher, so call "
                      "premiums look rich (24:03-26:07)."),
    ("His Excel routine", "Refresh every 3-5 min after 09:20; Ctrl+Q logs Nifty, Ctrl+W BankNifty; set the BankNifty "
                          "monthly expiry first or zeros get logged; NSE may lag 1-1.5 min; don't use the option-chain "
                          "and participant workbooks together (both use Ctrl+Q/W) (1:37-2:42, 30:04-34:55)."),
    ("Candles", "A candle is valid only if it breaks or equals the previous valid candle's high or low; closes don't "
                "matter; inside candles don't make swings. Same on every timeframe (49:04-1:00:04)."),
]


def _metric_card(col, title, value, good: bool | None, sub: str = ""):
    fill, text = (GOOD_FILL, GOOD_TEXT) if good else (BAD_FILL, BAD_TEXT) if good is False else ("#E7E6E6", "#3A3A3A")
    col.markdown(f"<div style='background:{fill};color:{text};padding:8px 10px;border-radius:6px'>"
                 f"<div style='font-size:0.8em'>{title}</div><div style='font-size:1.15em;font-weight:700'>{value}</div>"
                 f"<div style='font-size:0.78em'>{sub}</div></div>", unsafe_allow_html=True)


def _ladder_style(lad: pd.DataFrame):
    view = lad[["ce_oi", "ce_chg_oi", "ce_vol", "ce_ltp", "strike", "pe_ltp", "pe_vol", "pe_chg_oi", "pe_oi"]].copy()
    view.columns = ["CE OI", "CE chg OI", "CE vol", "CE LTP", "Strike", "PE LTP", "PE vol", "PE chg OI", "PE OI"]

    def paint(row):
        r = lad.loc[row.name]
        css = [""] * len(row)
        for i, c in enumerate(row.index):
            if c.startswith("CE") and r["ce_itm"]:
                css[i] = "background-color:#EDEDED;color:#222"
            if c.startswith("PE") and r["pe_itm"]:
                css[i] = "background-color:#EDEDED;color:#222"
            if c == "CE OI" and r["ce_max_oi"]:
                css[i] = f"background-color:{BAD_FILL};color:{BAD_TEXT};font-weight:700"
            if c == "PE OI" and r["pe_max_oi"]:
                css[i] = f"background-color:{BAD_FILL};color:{BAD_TEXT};font-weight:700"
            if c == "Strike":
                css[i] = "font-weight:700" + (";background-color:#FFF2CC;color:#222" if r["atm"] else "")
        return css
    sty = view.style.apply(paint, axis=1)
    sty = sty.bar(subset=["CE chg OI", "PE chg OI"], align="zero", color=[BAR_RED, BAR_GREEN])
    return sty.format({c: "{:,.0f}" for c in view.columns if c not in ("CE LTP", "PE LTP")} |
                      {"CE LTP": "{:.1f}", "PE LTP": "{:.1f}"}, na_rep="")


def _day_charts(tl: pd.DataFrame) -> go.Figure:
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, row_heights=[0.5, 0.25, 0.25], vertical_spacing=0.04,
                        specs=[[{"secondary_y": True}], [{}], [{}]],
                        subplot_titles=("Call chg OI vs Put chg OI (his graph)", "Net chg OI = PE - CE (green = put writers)",
                                        "Net volume = CE - PE (green = call volume higher)"))
    fig.add_trace(go.Scatter(x=tl["time"], y=tl["ce_chg_oi"], name="CE chg OI", line=dict(color=BAD_TEXT, width=2)), 1, 1)
    fig.add_trace(go.Scatter(x=tl["time"], y=tl["pe_chg_oi"], name="PE chg OI", line=dict(color=GOOD_TEXT, width=2)), 1, 1)
    fig.add_trace(go.Scatter(x=tl["time"], y=tl["spot"], name="Spot", line=dict(color="#1F4E79", width=1, dash="dot")),
                  1, 1, secondary_y=True)
    fig.add_trace(go.Bar(x=tl["time"], y=tl["net_chg_oi"], name="net chg OI", showlegend=False,
                         marker_color=[BAR_GREEN if v >= 0 else BAR_RED for v in tl["net_chg_oi"]]), 2, 1)
    fig.add_trace(go.Bar(x=tl["time"], y=tl["net_vol"], name="net vol", showlegend=False,
                         marker_color=[BAR_GREEN if v >= 0 else BAR_RED for v in tl["net_vol"]]), 3, 1)
    fig.update_layout(height=640, margin=dict(l=10, r=10, t=40, b=10), legend=dict(orientation="h", y=1.07), bargap=0)
    return fig


def _candle_chart(c: pd.DataFrame, zones: dict, src: str) -> go.Figure:
    v = m.valid_candles(c)
    fig = go.Figure()
    norm = v[~v["inside"]]
    fig.add_trace(go.Candlestick(x=norm["timestamp"], open=norm["open"], high=norm["high"], low=norm["low"],
                                 close=norm["close"], name="valid candles",
                                 increasing_line_color=GOOD_TEXT, decreasing_line_color=BAD_TEXT))
    ins = v[v["inside"]]
    if len(ins):
        fig.add_trace(go.Candlestick(x=ins["timestamp"], open=ins["open"], high=ins["high"], low=ins["low"],
                                     close=ins["close"], name="inside (ignored)",
                                     increasing_line_color="#000000", decreasing_line_color="#000000",
                                     increasing_fillcolor="#000000", decreasing_fillcolor="#000000"))
    sw = v[v["swing"] != ""]
    if len(sw):
        y = [r["high"] if r["swing"] == "H" else r["low"] for _, r in sw.iterrows()]
        fig.add_trace(go.Scatter(x=sw["timestamp"], y=y, mode="lines+markers+text", text=list(sw["swing"]),
                                 textposition="top center", name="structure", line=dict(color="#7030A0", width=1.5)))
    for key, colour in (("resistance_zone", BAD_FILL), ("support_zone", GOOD_FILL)):
        if key in zones:
            fig.add_hrect(y0=zones[key][0], y1=zones[key][1], fillcolor=colour, opacity=0.5, line_width=0,
                          annotation_text=key.replace("_", " "), annotation_position="top left")
    fig.update_yaxes(tickformat=",.0f")
    fig.update_layout(height=460, margin=dict(l=60, r=10, t=30, b=10), xaxis_rangeslider_visible=False,
                      title=f"15-min {src} candles — inside candles in black, structure from valid candles only")
    return fig


def render() -> None:
    st.subheader("Option chain — the trainer's method (Lecture 3)")
    R = m.rules()
    days = m._stored_days()
    if not days:
        st.info("No stored option-chain snapshots yet.")
        return
    c0, c1 = st.columns([1, 3])
    day = c0.selectbox("Session", list(reversed(days)), format_func=lambda d: d.strftime("%a %d-%b-%Y"))
    r = m.latest_reading(day, R)
    if not r:
        st.info("No snapshots that day.")
        return
    t, z = r["totals"], r["zones"]
    c1.caption(f"Latest snapshot {r['time']:%H:%M} · spot {r['spot']:,.1f} · expiry {r['expiry'] or '-'} · "
               f"{r['snapshots']} snapshots today")

    a, b, c, d, e = st.columns(5)
    _metric_card(a, "Writers (net chg OI)", t["writer_side"].title(), t["net_chg_oi"] > 0, f"{t['net_chg_oi']:+,.0f}")
    _metric_card(b, "Volume side (unproven)", t["volume_side"].title(), t["net_vol"] > 0, f"{t['net_vol']:+,.0f}")
    _metric_card(c, "Together", t["agreement"],
                 True if t["agreement"].startswith("BOTH GREEN") else False if t["agreement"].startswith("BOTH RED") else None)
    _metric_card(d, "PCR OI / vol / chg OI", f"{t['pcr_oi']:.2f} / {t['pcr_vol']:.2f} / {t['pcr_chg_oi']:.2f}", None)
    _metric_card(e, "Buildups ATM±300", r["buildup_verdict"].split(" (")[0],
                 True if r["buildup_verdict"].startswith("BULL") else False if r["buildup_verdict"].startswith("BEAR") else None,
                 r["buildup_verdict"].split(" (")[-1].rstrip(")"))

    st.markdown("#### Support / resistance from the chain")
    zc1, zc2, zc3 = st.columns(3)
    if "resistance_zone" in z:
        zc1.error(f"**Resistance zone {z['resistance_zone'][0]:,.0f} – {z['resistance_zone'][1]:,.0f}**  \n"
                  f"{z['resistance_strike']:,.0f} CE max OI + premium {z['resistance_premium']:.0f}. Above it: short covering.")
    if "support_zone" in z:
        zc2.success(f"**Support zone {z['support_zone'][0]:,.0f} – {z['support_zone'][1]:,.0f}**  \n"
                    f"{z['support_strike']:,.0f} PE max OI − premium {z['support_premium']:.0f}. Below it: long unwinding.")
    zc3.info(f"**Now:** {z['zone_state']}")

    st.markdown("#### In-the-money writers (trap watch)")
    if r["traps"]:
        st.dataframe(pd.DataFrame(r["traps"])[["side", "strike", "pts_itm", "chg_oi_day", "chg_oi_last", "state", "meaning"]],
                     use_container_width=True, hide_index=True)
    else:
        st.caption("No ITM writing 100–200 pts in the money right now — no trap setup (his 'no trap situation').")

    st.markdown("#### His ladder — 100-pt strikes, ATM ±300 (red = top-2 max OI, grey = ITM, yellow = ATM)")
    st.dataframe(_ladder_style(r["ladder"]), use_container_width=True, hide_index=True)

    tl = m.day_timeline(day)
    if len(tl) > 1:
        st.plotly_chart(_day_charts(tl), use_container_width=True)
    with st.expander("His Ctrl+Q log (every snapshot)"):
        st.dataframe(tl.iloc[::-1], use_container_width=True, hide_index=True)

    candles, src = m.load_candles(day, R["candles"]["timeframe_minutes"])
    if candles is not None and len(candles) > 2:
        st.plotly_chart(_candle_chart(candles, z, src), use_container_width=True)
    else:
        st.caption("No candles for this session yet.")

    with st.expander("Buildups by strike (day and last interval)"):
        st.dataframe(r["buildups"], use_container_width=True, hide_index=True)

    with st.expander("Does it work? History check on your stored snapshots"):
        if st.button("Run the history check (takes a minute)"):
            with st.spinner("Reading every stored session…"):
                tbl, nd = m.backtest(R)
            st.caption(f"{nd} stored sessions. Fewer than {R['backtest']['min_sample_size']} days = INSUFFICIENT SAMPLE.")
            if len(tbl):
                tbl = tbl.copy()
                tbl["Hit %"] = tbl["Hit %"].astype(str)
                tbl["All snapshots %"] = tbl["All snapshots %"].astype(str)
                st.dataframe(tbl, use_container_width=True, hide_index=True)
    with st.expander("His notes from this lecture"):
        for title, text in NOTES:
            st.markdown(f"**{title}.** {text}")


if __name__ == "__main__":
    st.set_page_config(page_title="Option chain — mentor", layout="wide")
    render()
