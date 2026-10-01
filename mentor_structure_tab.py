"""
mentor_structure_tab.py — Lecture 5 (market structure, inducement, imbalance, order blocks) as a page.

  .venv/bin/streamlit run mentor_structure_tab.py --server.port 8504
  or inside dashboard.py later:  import mentor_structure_tab; mentor_structure_tab.render()
"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import mentor_structure as ms

GOOD_FILL, GOOD_TEXT = "#C6EFCE", "#006100"
BAD_FILL, BAD_TEXT = "#FFC7CE", "#9C0006"

NOTES = [
    ("Break of structure", "Only a candle CLOSE beyond the previous high/low is a BOS. A wick through it is not "
                           "(15:27-15:43)."),
    ("Inducement", "The latest pullback of the current leg. A higher high is marked only after price takes the "
                   "inducement (a wick is enough); if price keeps rising, the inducement shifts to each newer "
                   "pullback. After the BOS, the lowest point of the range becomes the higher low (0:00-18:22)."),
    ("Change of character", "In a lower-low structure, breaking the last lower high = change of character "
                            "(1:54-2:18)."),
    ("Practice", "Missing a pullback breaks the whole structure. Practise on any chart until it is automatic "
                 "(17:50-18:31)."),
    ("Imbalance", "Three candles: bullish gap between candle-1 high and candle-3 low (only buyers there); bearish gap "
                  "between candle-1 low and candle-3 high. Size doesn't matter. The market usually comes back to fill "
                  "it, then continues; once filled it is used up (19:08-30:56)."),
    ("Imbalance signal", "Price wicks into an imbalance but closes back outside with a candle of the trend's colour -> "
                         "likely continuation, ~7/10 (31:03-32:17)."),
    ("Where zones are", "Bullish order blocks form near the bottom (around the higher low), bearish ones near the top. "
                        "Buy zones lie between the low and the inducement (35:02-38:16, 1:01:40)."),
    ("Order block", "1) the lowest candle of the pullback, any colour; 2) it sweeps the previous candle's low "
                    "(bonus, 50-50); 3) MOST IMPORTANT: an imbalance right after it. No imbalance -> shift to the next "
                    "candle, again and again, until one has it. Most people wait at the lowest candle and miss the "
                    "move (38:16-55:51)."),
    ("Zone", "Mark from the OB candle through its imbalance so the trade isn't missed; if the imbalance is very large, "
             "use the OB candle only (41:48-43:05)."),
    ("Which OB", "Of several OBs in a leg, the FIRST and the LAST hold most often; the middle ones rarely "
                 "(57:57-58:37)."),
    ("Trade", "Entry on the tap; SL below the OB candle; T1 = previous high (book most quantity); T2 = Fibonacci 1.2 of "
              "the leg. Engulfing / morning star at the zone can confirm a later entry. Strict SL, full confidence in "
              "the setup (32:29-35:02, 54:01-55:08, 1:02:15-1:03:42)."),
    ("Lecture 6 - drawing", "In TradingView draw each label (BOS, CHoCH, external liquidity sweep, inducement) with the "
                            "trend-line tool (magnet on, Shift for a straight line), text in the middle, and set its "
                            "VISIBILITY to that timeframe only; save it as a template per timeframe (BOS 15m, BOS 1D...). "
                            "He uses 15-min and day, sometimes week (0:00-8:24)."),
    ("Lecture 6 - start point", "Start from a place where the high and the low are clear, then continue the structure "
                                "every day (8:54-9:39)."),
    ("Lecture 6 - inside swings", "A swing that forms inside the previous swing's high-low range is like an inside "
                                  "candle: ignore it; only swings that break the range count (13:25-18:29). The app does "
                                  "this now (grey line = major swings)."),
    ("Lecture 6 - OB order", "Only after the inducement is taken: bearish -> mark OBs from the top down to the "
                             "inducement; bullish -> from the bottom up. An OB price already came into before that is "
                             "useless. He deletes the MIDDLE ones; FIRST ~50%, LAST kept. After the structure breaks "
                             "again the older OBs don't matter (19:06-27:51, 35:30-36:02)."),
    ("Lecture 6 - SL and re-entry", "The whole candle including the wick is the zone. SL as soon as price trades beyond "
                                    "it (not on a close). If that candle closes back inside (only a wick), take the trade "
                                    "again with the SL beyond the new wick. Book most at T1, some at Fib 1.2 (reached 6-7 "
                                    "of 10) (29:12-32:50)."),
    ("Lecture 6 - inducement sweep", "If price goes below the inducement but doesn't close below it (a wick), that is "
                                     "another buy for the previous high - 50-50 (1:07:35-1:08:30)."),
    ("Lecture 6 - shifting", "After a BOS, if price runs up with NO pullback and comes straight back below the marked "
                             "low, it is not a CHoCH: shift the high to the new top, the marked low becomes the "
                             "inducement, and the low goes back to the previous low. It can happen twice in a row "
                             "(1:06:09-1:15:17). Shown as SHIFT on the chart."),
    ("Final class - shifting recap", "Shifting happens only when the leg to the new high has NO pullback at all (so "
                                     "no inducement to draw) and price comes back below the low. You only know it "
                                     "when that low breaks. Mirror in a bearish market. Inside-bar indicator: "
                                     "inside_bars_mentor.pine (Pine Editor -> Add to chart) (0:09-17:40)."),
    ("Lecture 6 - options", "Writing is preferred. If buying: ITM or slightly ITM, never OTM, next week's expiry. Hedge "
                            "a written option with a far OTM buy (limits a gap loss, margin ~70-80k -> ~25k); exit both "
                            "at the SL. Trade 1 lot until profitable for a month, then 1 -> 2 -> 4 (42:38-48:18)."),
    ("Everywhere", "Same rules on every timeframe and market (forex, crypto, stocks, Nifty). Expect about 7 of 10 to "
                   "work (33:08-33:23, 1:03:42)."),
]


def _chart(c: pd.DataFrame, r: ms.Result, src: str) -> go.Figure:
    v = r.candles
    x = v["timestamp"]
    fig = go.Figure()
    norm = v[~v["inside"]]
    fig.add_trace(go.Candlestick(x=norm["timestamp"], open=norm["open"], high=norm["high"], low=norm["low"],
                                 close=norm["close"], name="candles",
                                 increasing_line_color=GOOD_TEXT, decreasing_line_color=BAD_TEXT))
    ins = v[v["inside"]]
    if len(ins):
        fig.add_trace(go.Candlestick(x=ins["timestamp"], open=ins["open"], high=ins["high"], low=ins["low"],
                                     close=ins["close"], name="inside (ignored)",
                                     increasing_line_color="#000", decreasing_line_color="#000",
                                     increasing_fillcolor="#000", decreasing_fillcolor="#000"))
    live_piv = [p for p in r.pivots if p.dropped_at is None]
    if live_piv:
        fig.add_trace(go.Scatter(x=[x.iloc[p.idx] for p in live_piv], y=[p.price for p in live_piv], mode="lines",
                                 line=dict(color="#A6A6A6", width=1), name="major swings (inside swings ignored)"))
    last = len(v) - 1
    for g in r.imbalances:
        end = g["filled_idx"] if g["filled_idx"] is not None else last
        if end - g["idx"] < 2:
            continue
        fig.add_shape(type="rect", x0=x.iloc[g["idx"]], x1=x.iloc[end], y0=g["bottom"], y1=g["top"], line_width=0,
                      fillcolor="#9DC3E6" if g["filled_idx"] is None else "#DDEBF7", opacity=0.35, layer="below")
    for o in r.obs:
        if o.result.startswith("CANCELLED"):
            continue
        end = o.tap_idx if o.tap_idx is not None else last
        col = GOOD_FILL if o.side == "bull" else BAD_FILL
        fig.add_shape(type="rect", x0=x.iloc[o.ob_idx], x1=x.iloc[end], y0=o.zone_lo, y1=o.zone_hi,
                      line=dict(color=GOOD_TEXT if o.side == "bull" else BAD_TEXT, width=1),
                      fillcolor=col, opacity=0.55)
        fig.add_annotation(x=x.iloc[o.ob_idx], y=o.zone_hi if o.side == "bull" else o.zone_lo,
                           text=f"OB {o.rank}" + (" · sweep" if o.sweep else ""), showarrow=False, font=dict(size=9),
                           yshift=8 if o.side == "bull" else -8)
    for e in r.events:
        if e["kind"] in ("HH", "HL", "LH", "LL"):
            up = e["kind"] in ("HH", "LH")
            fig.add_annotation(x=x.iloc[e["idx"]], y=e["price"], text=e["kind"], showarrow=False,
                               yshift=12 if up else -12, font=dict(size=11, color="#1F4E79"))
        elif e["kind"] in ("BOS_UP", "BOS_DN", "CHOCH_UP", "CHOCH_DN", "SHIFT"):
            fig.add_annotation(x=x.iloc[e["idx"]], y=e["price"], text=e["kind"].replace("_", " "),
                               showarrow=True, arrowhead=2, ax=0, ay=-25 if "UP" in e["kind"] else 25,
                               font=dict(size=10, color="#7030A0"))
    st_ = r.state
    if st_ and st_.get("inducement"):
        fig.add_hline(y=st_["inducement"][1], line_dash="dot", line_color="#C65911",
                      annotation_text="inducement", annotation_position="bottom right")
    fig.update_yaxes(tickformat=",.0f")
    fig.update_layout(height=620, margin=dict(l=60, r=10, t=35, b=10), xaxis_rangeslider_visible=False,
                      title=f"15-min {src}: structure from valid candles · BOS/CHoCH on closes · OB zones · imbalances",
                      xaxis=dict(rangebreaks=[dict(bounds=[15.5, 9.25], pattern="hour"), dict(bounds=["sat", "mon"])]),
                      legend=dict(orientation="h", y=-0.06))
    return fig


def render() -> None:
    st.subheader("Market structure & order blocks — the trainer's method (Lectures 5 + 6)")
    R = ms.rules()
    c1, c2 = st.columns([1, 4])
    days = c1.selectbox("Sessions", [3, 5, 10, 20], index=1)
    c, src = ms.recent_candles(days, R)
    if c is None or len(c) < 20:
        st.info("No 15-min candles available (check the Dhan token / candle source).")
        return
    r = ms.analyse(c, R)
    ms.simulate(c, r, R)
    s = r.state
    trend = {1: ("BULLISH — HH / HL", True), -1: ("BEARISH — LH / LL", False), 0: ("UNDECIDED", None)}[s.get("trend", 0)]
    fill, text = (GOOD_FILL, GOOD_TEXT) if trend[1] else (BAD_FILL, BAD_TEXT) if trend[1] is False else ("#E7E6E6", "#333")
    c2.markdown(f"<div style='background:{fill};color:{text};padding:8px 12px;border-radius:6px'>"
                f"<b>{trend[0]}</b> · high {s['high'][1]:,.1f} · low {s['low'][1]:,.1f}"
                + (f" · inducement {s['inducement'][1]:,.1f}" if s.get("inducement") else "")
                + (" · leg confirmed (inducement taken)" if s.get("confirmed") else " · waiting for the inducement")
                + f" · last {c['close'].iloc[-1]:,.1f}</div>", unsafe_allow_html=True)
    st.plotly_chart(_chart(c, r, src), use_container_width=True)

    act = [o for o in r.obs if o.tap_idx is None and not o.result.startswith("CANCELLED")]
    rows = []
    for o in r.obs[-12:]:
        entry = o.zone_hi if o.side == "bull" else o.zone_lo
        risk = abs(entry - o.sl)
        rows.append({"Side": "BUY" if o.side == "bull" else "SELL", "Rank": o.rank, "Sweep": "yes" if o.sweep else "",
                     "Shifted candles": o.shifted, "Zone": f"{o.zone_lo:,.0f} – {o.zone_hi:,.0f}",
                     "Entry": round(entry), "SL": round(o.sl), "T1 (prev extreme)": round(o.t1),
                     "T2 (Fib 1.2)": round(o.t2), "R:R to T1": round(abs(o.t1 - entry) / risk, 1) if risk else None,
                     "Status": (o.result if o.result.startswith("CANCELLED") else "WAITING") if o.tap_idx is None
                     else (o.result or "TAPPED"),
                     "His view": "he deletes MIDDLE ones" if o.rank == "MIDDLE" else ""})
    st.markdown(f"#### Order-block zones ({len(act)} waiting for a tap)")
    if rows:
        st.dataframe(pd.DataFrame(rows[::-1]), use_container_width=True, hide_index=True)
    else:
        st.caption("No order block meets his three conditions in this window yet.")

    with st.expander("Does it work? History check on 1 year of Nifty 15-min candles (Dhan)"):
        if st.button("Run the history check"):
            with st.spinner("Loading 15-min history from Dhan and replaying his rules…"):
                b = ms.backtest(R)
            if "error" in b:
                st.warning(b["error"])
            else:
                st.caption(f"{b['bars']:,} bars, {b['from']:%d-%b-%Y} to {b['to']:%d-%b-%Y}; {b['legs']} confirmed legs, "
                           f"{b['obs']} order blocks. SL counted first if one candle touches both. The CONTROL row is "
                           f"the same bars shuffled: a real edge must beat it.")
                ob = b["order_blocks"].copy()
                for col in ob.columns[2:]:
                    ob[col] = ob[col].astype(str)
                st.dataframe(ob, use_container_width=True, hide_index=True)
                if "lecture6" in b and len(b["lecture6"]):
                    st.markdown("**Lecture 6 checks** (inside swings ignored, shifting, old OBs cancelled, re-entry, "
                                "inducement sweep)")
                    l6 = b["lecture6"].copy()
                    for col in l6.columns[1:]:
                        l6[col] = l6[col].astype(str)
                    st.dataframe(l6, use_container_width=True, hide_index=True)
                im = b["imbalances"].copy()
                if len(im):
                    im[im.columns[2]] = im[im.columns[2]].astype(str)
                st.dataframe(im, use_container_width=True, hide_index=True)
    with st.expander("His notes from this lecture"):
        for title, t in NOTES:
            st.markdown(f"**{title}.** {t}")


if __name__ == "__main__":
    st.set_page_config(page_title="Structure — mentor", layout="wide")
    render()
