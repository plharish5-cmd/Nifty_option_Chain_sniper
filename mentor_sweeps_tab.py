"""
mentor_sweeps_tab.py — Lecture 7 (double bottoms, sweeps, voids, swing range, stops, higher timeframe).

  .venv/bin/streamlit run mentor_sweeps_tab.py --server.port 8505
  or inside dashboard.py later:  import mentor_sweeps_tab; mentor_sweeps_tab.render()
"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import mentor_structure as ms
import mentor_sweeps as msw

GOOD_FILL, GOOD_TEXT = "#C6EFCE", "#006100"
BAD_FILL, BAD_TEXT = "#FFC7CE", "#9C0006"
NEUTRAL_FILL, NEUTRAL_TEXT = "#E7E6E6", "#333333"

NOTES = [
    ("Double bottom (two-candle zone)", "Use it when no order block is available. A bottom candle, any colour (ideally it "
     "sweeps the previous low; an imbalance after it is a bonus), then a candle that CLOSES above it. The whole bottom "
     "candle is the buy zone. Buy on the retest, no extra confirmation. SL below the bottom candle. Target = the latest "
     "swing high. Works on 15-min, day and week (0:30-13:25)."),
    ("1:1 minimum", "Mark the zone and the target first: if the reward is smaller than the risk (50 points risk for 30 "
     "points reward) skip it (11:27-11:48, 25:02-25:43)."),
    ("No emotions", "SL hit -> exit, even if every condition matched. If a wick forms you'll see it at the day's close "
     "and can re-enter then (23:30-24:39)."),
    ("Wick void", "When a bottom candle is an inside candle (so no order block), the unfilled wick-to-wick gap above the "
     "bottom can still act as the last buy zone above the low (14:02-16:18)."),
    ("Bigger timeframe = writing", "Day/week targets can take weeks or months (a weekly target hit 3 months later), so "
     "write options with a fixed SL at the same level: put writing at a buy zone, call writing at a sell zone - "
     "sideways still pays theta (16:24-19:59)."),
    ("Which contract", "For day-chart trades take next month or the one after. If the signal comes right at the start "
     "of the month (~20 days left) the current month works - e.g. a monthly candle that swept the previous month's high "
     "with an evening star -> month-end call writing (20:08-21:34)."),
    ("Void close rule", "The close-to-close void can be a buy zone: no close below it -> reversal up; a close below it -> "
     "market wants lower (22:16-22:59)."),
    ("Sweep = intent", "A candle that goes above the previous candle's high and closes back below shows intent DOWN - at "
     "least to that range's low. Sweeping a low and closing above = intent UP. Same on 1-min, 15-min, day, week, month; "
     "only the distance changes (27:58-32:29, 51:48-52:47)."),
    ("Three-candle resistance", "When the top candle is red and swept a high, the area from its top to the next candle's "
     "top can act as resistance (28:43-29:32)."),
    ("Previous day high/low", "They matter most. Low breaks -> ready to fall; high breaks -> ready to rise. Previous-day "
     "high swept and closed below -> next day bias down (36:17-39:57, 49:24-49:54)."),
    ("Swing range", "Mark the latest 15-min swing high and low. An open inside them is a useless market (call writers "
     "above, put writers below). A 15-min close above the swing high -> T1 = lowest point of the imbalance above; SL "
     "below the level. Don't short until the low breaks (40:05-42:21, 47:27-48:48)."),
    ("Stops as targets", "Untouched swing lows are buyers' stops. When many sit close together the market often takes "
     "them all in one or two candles (the 4-June candle) - news is just the excuse (33:00-35:02, 54:04-57:58)."),
    ("Gaps", "The first time price comes back into a 15-min gap it usually does NOT fill it (7-8 of 10): it takes "
     "support and goes on; later it may break it (59:47-1:01:44)."),
    ("Volume", "At a real bottom volume is high: the high-volume candle is usually the LAST one, then momentum. High "
     "volume on a red candle near a top -> may fall (1:03:05-1:05:45). Needs futures candles (the index has no volume)."),
    ("Higher timeframe wins", "A 15-min bullish setup failed because the day chart was bearish: what the bigger "
     "timeframe says is followed on the smaller one (1:06:42-1:08:20)."),
    ("Portfolio", "Exit a portfolio only near Fibonacci 1.4 when the data turns negative (stock futures emptied). Three "
     "buckets: investment, swing (~1 month), intraday (day + 15-min) (49:54-50:09, 55:38-56:11)."),
    ("Lecture 6 - body voids", "Yellow on his chart: the gap between two candles' BODIES (a close and the next open). "
     "Price comes back into it; if it doesn't close beyond it, trade away from it with the SL beyond that wick, target "
     "the previous high/low. A close beyond it = no trade. Size doesn't matter; some fail. Use it while the 15-min "
     "structure isn't complete (48:18-59:46)."),
    ("Lecture 6 - volume-profile divergence", "His own finding: day 1 closes above its highest-volume price (POC); day "
     "2 closes below day 1's close while its POC moves up. Next day usually has momentum - he buys a call and a put "
     "before the close. Fixed-range volume profile in TradingView (1:02:06-1:06:05)."),
    ("Lecture 6 - options", "Writing preferred. Buying: ITM/slightly ITM, next week's expiry. Hedge writing with a far "
     "OTM buy (margin ~70-80k -> ~25k). 1 lot until profitable for a month, then 1 -> 2 -> 4 (42:38-48:18)."),
    ("Final class - double bottom", "Only the SECOND candle matters: it closes above the bottom candle (the bottom "
     "candle's colour doesn't). Enter in the bottom candle, exit when a candle CLOSES below it, target the previous "
     "high. People wait for the neckline break and miss the move. Day double bottom + 15-min sweep = higher "
     "probability. A wide zone can be refined on 5-min, but then it may be missed (20:09-35:22)."),
    ("Final class - targets", "Targets where candle BODIES closed, not the wick extremes. Work top-down: data (FII "
     "PCR < 1 = bearish) -> day chart -> 15-min. Draw levels on futures, choose strikes from spot (40:40, 42:57, 1:15:49)."),
    ("Final class - volume", "He has seen the high-volume rule only with RED candles at a bottom, not with green "
     "candles at a top (18:06-19:22)."),
    ("Final class - RSI divergences", "Tops: price higher high, RSI lower high -> down. Lows: price higher low, RSI "
     "lower low -> up (positive). Highs: price lower high, RSI higher high -> down (negative). 7-8 of 10 (1:03:24-1:10:13)."),
    ("Final class - monthly-expiry straddle", "Open of the Nifty monthly-expiry day: buy a next-week call + put ~50 "
     "pts OTM; futures move +-300 from that open (Bank Nifty +-600). Exit both together at the target. Low VIX "
     "(11-12) best; skip high VIX and the October week (1:14:28-1:26:19)."),
    ("Final class - sizing", "1 lot (now 75) until profitable for a month, then 1 -> 2 -> 4 -> 8 -> 16. Pick up extra "
     "ideas elsewhere only if they don't clash with a setup that already works for you (1:31:04, 1:34:00)."),
    ("Practice", "Some days there is no setup - don't force a trade. Backtest these, practise on weekends, connect the "
     "dots (13:31, 52:47-53:49, 1:01:18)."),
]


VERD = {"rules": {}}


def _v(*keys: str) -> str:
    """Small backtest badges for the given rules (latest run, else the 28-Sep-2026 run)."""
    out = []
    for k in keys:
        x = VERD.get("rules", {}).get(k)
        if not x:
            continue
        col = {1: GOOD_TEXT, -1: BAD_TEXT}.get(x.get("level", 0), "#595959")
        out.append(f"<span style='font-size:0.8em;color:{col}'>&#9632; <b>Backtest</b> {x['rule']}: "
                   f"<b>{x['verdict']}</b> ({x['detail']})</span>")
    return "<br>".join(out)


def _card(col, title: str, body: str, bias: int) -> None:
    fill, text = (GOOD_FILL, GOOD_TEXT) if bias > 0 else (BAD_FILL, BAD_TEXT) if bias < 0 else (NEUTRAL_FILL, NEUTRAL_TEXT)
    col.markdown(f"<div style='background:{fill};color:{text};padding:8px 10px;border-radius:6px;min-height:92px'>"
                 f"<b>{title}</b><br>{body}</div>", unsafe_allow_html=True)


def _chart(c: pd.DataFrame, L: dict, src: str) -> go.Figure:
    x = c["timestamp"]
    fig = go.Figure(go.Candlestick(x=x, open=c["open"], high=c["high"], low=c["low"], close=c["close"], name="15-min",
                                   increasing_line_color=GOOD_TEXT, decreasing_line_color=BAD_TEXT))
    last = len(c) - 1
    z = L.get("zones")
    if z is not None and len(z):
        for _, r in z.iterrows():
            bull = r["side"] == "bull"
            fig.add_shape(type="rect", x0=x.iloc[int(r["bottom_idx"])], x1=x.iloc[last], y0=r["zone_lo"], y1=r["zone_hi"],
                          line=dict(color=GOOD_TEXT if bull else BAD_TEXT, width=1),
                          fillcolor=GOOD_FILL if bull else BAD_FILL, opacity=0.5)
            fig.add_annotation(x=x.iloc[int(r["bottom_idx"])], y=r["zone_lo"] if bull else r["zone_hi"],
                               text="double bottom" if bull else "double top", showarrow=False, font=dict(size=9),
                               yshift=-8 if bull else 8)
    bv = L.get("body_voids")
    if bv is not None and len(bv):
        for _, r in bv.tail(10).iterrows():
            f = int(r["formed_idx"])
            if f >= len(c):
                continue
            fig.add_shape(type="rect", x0=x.iloc[max(0, f - 1)], x1=x.iloc[last], y0=r["bottom"], y1=r["top"], line_width=0,
                          fillcolor="#FFD966", opacity=0.45, layer="below")
    v = L.get("voids")
    if v is not None and len(v):
        for _, r in v.tail(8).iterrows():
            f = int(r["formed_idx"])
            if f >= len(c):
                continue
            fig.add_shape(type="rect", x0=x.iloc[max(0, f - 2)], x1=x.iloc[last], y0=r["bottom"], y1=r["top"], line_width=0,
                          fillcolor="#9DC3E6", opacity=0.3, layer="below")
    tr = L.get("today_range")
    if tr:
        for y, name in ((tr["swing_high"], "15m swing high"), (tr["swing_low"], "15m swing low")):
            fig.add_hline(y=y, line_dash="dash", line_color="#7030A0", annotation_text=name,
                          annotation_position="top left")
    s = L.get("stops") or {}
    for key, col in (("cluster_below", "#C65911"), ("cluster_above", "#C65911")):
        for y in s.get(key) or []:
            fig.add_hline(y=y, line_dash="dot", line_color=col, opacity=0.6)
    sw = L.get("sweeps")
    if sw is not None and len(sw):
        for _, r in sw.iterrows():
            i = int(r["idx"])
            if i >= len(c):
                continue
            up = r["side"] == "bull"
            fig.add_annotation(x=x.iloc[i], y=c["low"].iloc[i] if up else c["high"].iloc[i],
                               text=("▲" if up else "▼") + (" swing" if r["kind"] == "swing" else ""), showarrow=False,
                               yshift=-12 if up else 12, font=dict(size=10, color=GOOD_TEXT if up else BAD_TEXT))
    fig.update_yaxes(tickformat=",.0f")
    fig.update_layout(height=600, margin=dict(l=60, r=10, t=35, b=10), xaxis_rangeslider_visible=False,
                      title=f"15-min {src}: two-candle zones · body voids (yellow) · wick voids (blue) · swing range (purple) · stops (orange) · ▲▼ sweeps",
                      xaxis=dict(rangebreaks=[dict(bounds=[15.5, 9.25], pattern="hour"), dict(bounds=["sat", "mon"])]),
                      legend=dict(orientation="h", y=-0.06))
    return fig


def render() -> None:
    st.subheader("Double bottoms, sweeps, voids & the bigger timeframe — the trainer's method (Lectures 6 + 7)")
    R = msw.rules()
    c1, _ = st.columns([1, 4])
    days = c1.selectbox("Sessions", [3, 5, 10], index=1)
    c, src = ms.recent_candles(days)
    if c is None or len(c) < 20:
        st.info("No 15-min candles available (check the Dhan token / candle source).")
        return
    c = c.reset_index(drop=True)
    try:
        import participant_correlation as pc
        dd = pc.load_ohlc()
        daily = msw.daily_frame(dd) if dd is not None else None
    except Exception as exc:
        st.caption(f"Daily history unavailable ({exc}); day/week/month cards hidden.")
        daily = None
    L = msw.live(c, daily, R)
    VERD.clear()
    VERD.update(msw.load_verdicts(R))
    badge = {"day": ("swing_sweep_1d", "candle_sweep_1d"), "week": ("sweep_1w",), "month": ()}

    cols = st.columns(4)
    for col, k in zip(cols[:3], ("month", "week", "day")):
        x = L.get(k)
        if x:
            when = f"week to {x['start']:%d-%b}" if k == "week" else f"{x['start']:%b-%Y}" if k == "month" else f"{x['start']:%d-%b}"
            extra = _v(*badge[k]) or ("<span style='font-size:0.8em;color:#595959'>&#9632; <b>Backtest</b>: only ~90 "
                                      "months of data - not enough to test</span>" if k == "month" else "")
            _card(col, f"{k.title()} candle ({when})", x["text"] + ("<br>" + extra if extra else ""), x["bias"])
    trend = L.get("day_trend_now")
    if trend is not None:
        _card(cols[3], "Day structure (decides the 15-min)",
              {1: "BULLISH: prefer 15-min buys", -1: "BEARISH: prefer 15-min sells", 0: "undecided"}[trend]
              + (f"<br><small>Contract: {L['contract']}</small>" if L.get("contract") else "")
              + ("<br>" + _v("double_with_day_15m") if _v("double_with_day_15m") else ""), trend)

    tr = L.get("today_range")
    if tr:
        inside = tr["open_is"] == "INSIDE"
        msg = (f"Latest 15-min swing range {tr['swing_low']:,.0f} – {tr['swing_high']:,.0f}; today opened "
               f"<b>{tr['open_is']}</b>" + (" = useless market until a 15-min close outside it" if inside else "")
               + (f"; broke <b>{tr['break']}</b>" + (f" (R:R {tr['rr']}, T1 {tr['target']:,.0f})" if pd.notna(tr.get("rr")) and tr.get("target") is not None else "") if tr["break"] else ""))
        b = 1 if tr["break"] == "UP" else -1 if tr["break"] == "DOWN" else 0
        _card(st, "Swing range today", msg + ("<br>" + _v("useless_open", "swing_break") if _v("useless_open", "swing_break")
                                               else ""), b)
    vc = L.get("volume_climax")
    st.caption("Volume climax: " + ("no volume in these candles (index) - needs futures candles" if vc is None
                                     else "HIGH-VOLUME candle in the last 3 bars - could be the last candle of the move" if vc
                                     else "none in the last 3 bars")
               + (" · Pattern on the last candle: " + ", ".join(k.replace('_', ' ') for k in L["patterns"])
                  if L.get("patterns") else ""))
    st.plotly_chart(_chart(c, L, src), use_container_width=True)
    ch = _v("swing_sweep_15m", "swing_sweep_major_15m", "candle_sweep_15m", "gap_first_return", "void_close_rule")
    if ch:
        st.markdown("▲▼ <b>swing</b> markers = the rule that beat random (use with the day trend); plain ▲▼ = candle sweeps."
                    "<br>" + ch, unsafe_allow_html=True)

    bs = L.get("body_signals")
    st.markdown("#### Body-void signals, last 26 candles (Lecture 6)")
    b6 = _v("body_void_15m")
    if b6:
        st.markdown(b6, unsafe_allow_html=True)
    if bs is not None and len(bs):
        st.dataframe(pd.DataFrame([{"Side": "BUY" if r["side"] == "bull" else "SELL",
                                    "Time": f"{c['timestamp'].iloc[int(r['touch_idx'])]:%d-%b %H:%M}",
                                    "Void": f"{r['bottom']:,.1f} – {r['top']:,.1f}", "Entry": round(r["entry"]),
                                    "SL (wick)": round(r["sl"]), "Target (previous extreme)": round(r["target"]),
                                    "R:R": r["rr"], "His 1:1 rule": "OK" if r["passes_rr"] else "SKIP",
                                    "So far": r["result"]} for _, r in bs.iloc[::-1].iterrows()]),
                     use_container_width=True, hide_index=True)
    else:
        st.caption("No body-void rejection in the last 26 candles.")
    vp = L.get("vp")
    if vp and vp.get("divergence"):
        _card(st, "Volume-profile divergence (futures volume)",
              f"{vp['day']}: <b>{vp['divergence']}</b> (close {vp['close']:,.0f}, POC {vp['poc']:,.0f}) -> he expects "
              f"momentum next day and buys a call and a put." + ("<br>" + _v("vp_divergence") if _v("vp_divergence") else ""), 0)
    elif vp:
        st.caption(f"Volume profile {vp['day']}: close {vp['close']:,.0f}, POC {vp['poc']:,.0f} - no divergence. "
                   + (_v("vp_divergence").replace("<br>", " ") if _v("vp_divergence") else ""), unsafe_allow_html=True)
    else:
        st.caption("Volume profile: no stored futures candles with volume found.")

    z = L.get("zones")
    st.markdown(f"#### Two-candle zones waiting for a retest ({0 if z is None else len(z)})")
    if _v("double_15m", "reentry_15m"):
        st.markdown(_v("double_15m", "double_1d", "reentry_15m"), unsafe_allow_html=True)
    if z is not None and len(z):
        rows = []
        for _, r in z.iloc[::-1].iterrows():
            bull = r["side"] == "bull"
            entry = r["zone_hi"] if bull else r["zone_lo"]
            sl = r["zone_lo"] - R["risk"]["sl_buffer_points"] if bull else r["zone_hi"] + R["risk"]["sl_buffer_points"]
            rr = abs(r["target"] - entry) / abs(entry - sl) if entry != sl else 0
            rows.append({"Side": "BUY" if bull else "SELL", "Formed": f"{c['timestamp'].iloc[int(r['formed_idx'])]:%d-%b %H:%M}",
                         "Zone": f"{r['zone_lo']:,.0f} – {r['zone_hi']:,.0f}", "Entry (retest)": round(entry),
                         "SL": round(sl), "Target (swing)": round(r["target"]), "R:R": round(rr, 1),
                         "His 1:1 rule": "OK" if rr >= R["risk"]["min_rr"] else "SKIP",
                         "Imbalance": "yes" if r["imbalance"] else "", "Pattern": r["pattern"]})
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.caption("None open in this window.")
    s = L.get("stops") or {}
    st.markdown("#### Stop pools (untouched swings)")
    st.caption("Not backtested yet - his idea, shown as levels only.")
    st.write(f"Below (buyers' stops): {', '.join(f'{x:,.0f}' for x in s.get('below', [])[:8]) or '–'}"
             + (f"  ·  **cluster** {', '.join(f'{x:,.0f}' for x in s['cluster_below'])}" if s.get("cluster_below") else ""))
    st.write(f"Above (sellers' stops): {', '.join(f'{x:,.0f}' for x in s.get('above', [])[:8]) or '–'}"
             + (f"  ·  **cluster** {', '.join(f'{x:,.0f}' for x in s['cluster_above'])}" if s.get("cluster_above") else ""))

    with st.expander("Does it work? History check (15-min: 1 year from Dhan; day/week/month: since 2019)"):
        if VERD.get("rules"):
            st.caption(f"Verdicts on the cards come from the run of {VERD.get('run', '?')} ({VERD.get('data', '')}). "
                       "Running the check again updates them.")
            st.dataframe(pd.DataFrame([{"Rule": x["rule"], "Verdict": x["verdict"], "Numbers": x["detail"]}
                                       for x in VERD["rules"].values()]), use_container_width=True, hide_index=True)
        if st.button("Run the history check", key="sweeps_bt"):
            with st.spinner("Replaying his Lecture 7 rules on history and on shuffled bars…"):
                b = msw.backtest(R)
            if "error" in b:
                st.warning(b["error"])
            else:
                st.caption(" · ".join(f"{tf}: {n:,} bars {f:%d-%b-%Y}..{t:%d-%b-%Y}" for tf, (f, t, n) in b["span"].items())
                           + ". SL counted first when one candle touches both. CONTROL rows = the same bars shuffled; "
                             "a real edge must beat them. Avg R > 0 means the setup made money on average.")
                for key, title in (("double", "Double bottom / top"), ("sweeps", "Sweeps (candle and swing)"),
                                   ("voids", "Voids: first return and the close rule"),
                                   ("swing_range", "Swing range at the open"),
                                   ("body_voids", "Lecture 6: body voids"),
                                   ("volume", "Lecture 6/7: volume rules (futures volume)")):
                    if key not in b:
                        continue
                    t = b[key].copy()
                    for col in t.columns:
                        if t[col].dtype == object:
                            t[col] = t[col].astype(str)
                    st.markdown(f"**{title}**")
                    st.dataframe(t, use_container_width=True, hide_index=True)
    with st.expander("Final class: RSI divergences, double-bottom exit, monthly-expiry straddle"):
        try:
            import mentor_extras as mx
            X = mx.live(c, daily)
            for tf in ("15m", "1d"):
                if f"rsi_{tf}" in X:
                    dv = X.get(f"div_{tf}")
                    st.write(f"RSI {tf}: {X[f'rsi_{tf}']:.1f}" + (" · recent divergences:" if dv is not None and len(dv) else
                                                              " · no divergence in the latest candles"))
                    if dv is not None and len(dv):
                        st.dataframe(dv[["kind", "side", "entry", "sl", "target", "rr", "result"]].iloc[::-1],
                                     use_container_width=True, hide_index=True)
            if st.button("Run the history check (final class)", key="extras_bt"):
                with st.spinner("Testing divergences, the close-based double-bottom exit and the expiry straddle…"):
                    b = mx.backtest()
                for key, title in (("divergence", "RSI divergences"), ("double_close", "Double bottom: close vs touch exit"),
                                   ("expiry", "Monthly-expiry straddle and red-expiry BTST (daily, since 2019)")):
                    t = b[key].copy()
                    for col in t.columns:
                        if t[col].dtype == object:
                            t[col] = t[col].astype(str)
                    st.markdown(f"**{title}**")
                    st.dataframe(t, use_container_width=True, hide_index=True)
        except Exception as exc:
            st.caption(f"Final-class module unavailable: {exc}")
    with st.expander("His notes from this lecture"):
        for title, t in NOTES:
            st.markdown(f"**{title}.** {t}")


if __name__ == "__main__":
    st.set_page_config(page_title="Double bottoms — mentor", layout="wide")
    render()
