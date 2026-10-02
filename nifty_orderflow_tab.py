"""
nifty_orderflow_tab.py — 🌊 NIFTY 50 Order Flow tab (replaces the old simulated market_depth_tab view).

Shows ONLY real recorded data from orderflow_recorder.py. If the recorder has not run, the tab says so
and shows how to start it — it never draws made-up footprints, walls or CVD.
Information only — not financial advice.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

import nifty_orderflow as nf

NAVY, PANEL, GRID = "#0b1a33", "#10223f", "#1e3358"
GOLD, WHITE, MUTED = "#d4af37", "#f1f5f9", "#94a3b8"
GREEN, RED, BLUE, AMBER, PURPLE = "#22c55e", "#ef4444", "#38bdf8", "#f59e0b", "#a78bfa"
MARK = {"stacked_buy": ("triangle-up", GREEN), "stacked_sell": ("triangle-down", RED),
        "absorb_bull": ("diamond", GREEN), "absorb_bear": ("diamond", RED),
        "div_bull": ("circle", "#86efac"), "div_bear": ("circle", "#fca5a5"),
        "cvd_div_bull": ("star", GREEN), "cvd_div_bear": ("star", RED)}


def _cr(x: float) -> str:
    return f"₹{x:+,.0f} Cr" if abs(x) >= 10 else f"₹{x:+,.1f} Cr"


def _layout(fig, title: str, h: int = 380):
    fig.update_layout(title=dict(text=f"<b>{title}</b>", font=dict(color=GOLD, size=15), x=0.01),
                      paper_bgcolor=NAVY, plot_bgcolor=PANEL, font=dict(color=WHITE, size=12), height=h,
                      margin=dict(l=55, r=20, t=50, b=30), hovermode="x unified",
                      legend=dict(font=dict(color=WHITE, size=11), bgcolor="rgba(11,26,51,0.85)", x=1.01, y=1,
                                  xanchor="left", yanchor="top"))
    fig.update_xaxes(gridcolor=GRID, zeroline=False)
    fig.update_yaxes(gridcolor=GRID, zeroline=False)
    return fig


def _card(title: str, value: str, sub: str, col: str) -> str:
    return (f"<div style='background:{PANEL};border:1px solid {GRID};border-top:3px solid {col};border-radius:8px;"
            f"padding:10px 12px;height:100%'><div style='color:{MUTED};font-size:0.75rem;font-weight:700'>{title}</div>"
            f"<div style='color:{col};font-size:1.35rem;font-weight:800;margin-top:2px'>{value}</div>"
            f"<div style='color:#cbd5e1;font-size:0.78rem;margin-top:2px'>{sub}</div></div>")


def _badge(kind: str, val: dict) -> str:
    s = nf.status_of(kind, val)
    col = GREEN if s == "VALIDATED" else RED if s == "FAILED" else AMBER
    return f"<span style='background:{col};color:#000;font-weight:800;font-size:0.68rem;padding:1px 6px;border-radius:4px'>{s}</span>"


# ═══════════════════════════════════════════════════════════════════════════
# Charts
# ═══════════════════════════════════════════════════════════════════════════
def chart_main(f: nf.Flow, n_show: int, val: dict):
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    c = f.candles.tail(n_show)
    i0 = c.index.min()
    fig = make_subplots(rows=4, cols=1, shared_xaxes=True, vertical_spacing=0.025, row_heights=[0.52, 0.16, 0.18, 0.14],
                        subplot_titles=("", "Delta per candle (₹ Cr) — aggressive buy − sell", "Cumulative delta (₹ Cr)",
                                        "Breadth — stocks net buying minus net selling"))
    fig.add_trace(go.Candlestick(x=c["t"], open=c["o"], high=c["h"], low=c["l"], close=c["c"], name="NIFTY 50",
                                 increasing_line_color=GREEN, decreasing_line_color=RED), 1, 1)
    fig.add_trace(go.Scatter(x=c["t"], y=c["poc"], mode="markers", name="Candle POC",
                             marker=dict(symbol="line-ew-open", size=12, color=GOLD, line=dict(width=2, color=GOLD))), 1, 1)
    x_end = c["t"].iloc[-1] + pd.Timedelta(minutes=f.minutes)
    for z in f.zones:
        if z["i"] < i0:
            continue
        end = z.get("broken_t", x_end) if not z["active"] else x_end
        col = GREEN if z["kind"] == "stacked_buy" else RED
        fig.add_shape(type="rect", x0=z["t"], x1=end, y0=z["lo"], y1=z["hi"], line=dict(width=0),
                      fillcolor=col, opacity=0.22 if z["active"] else 0.08, row=1, col=1)
    ev = f.events[f.events["i"] >= i0] if len(f.events) else f.events
    for kind, g in (ev.groupby("kind") if len(ev) else []):
        sym, col = MARK.get(kind, ("circle", WHITE))
        bull = kind in nf.BULLISH
        y = [c.loc[i, "l"] - 0.15 * max(c["range"].median(), 1) if bull else c.loc[i, "h"] + 0.15 * max(c["range"].median(), 1)
             for i in g["i"]]
        fig.add_trace(go.Scatter(x=g["t"], y=y, mode="markers", name=f"{nf.SIGNALS[kind][0]} ({nf.status_of(kind, val)})",
                                 marker=dict(symbol=sym, size=11, color=col, line=dict(width=1, color="#000")),
                                 text=g["detail"], hovertemplate="%{text}<extra>" + nf.SIGNALS[kind][0] + "</extra>"), 1, 1)
    if f.poc:
        fig.add_hline(y=f.poc, line=dict(color=GOLD, dash="dot", width=1), annotation_text=f"Day POC {f.poc:,.0f}",
                      annotation_font_color=GOLD, row=1, col=1)
    fig.add_trace(go.Bar(x=c["t"], y=c["delta"], name="Delta", marker_color=np.where(c["delta"] >= 0, GREEN, RED),
                         showlegend=False), 2, 1)
    fig.add_trace(go.Scatter(x=c["t"], y=c["cvd"], name="CVD", line=dict(color=BLUE, width=2), fill="tozeroy",
                             fillcolor="rgba(56,189,248,0.12)", showlegend=False), 3, 1)
    net = c["breadth_up"] - c["breadth_dn"]
    fig.add_trace(go.Bar(x=c["t"], y=net, name="Breadth", marker_color=np.where(net >= 0, GREEN, RED), showlegend=False), 4, 1)
    fig.update_layout(xaxis_rangeslider_visible=False)
    for a in fig.layout.annotations:
        a.font = dict(color=MUTED, size=11)
    return _layout(fig, f"NIFTY 50 · {f.minutes}-min · order flow of the 50 index stocks", 860)


def chart_heat(f: nf.Flow, n_show: int):
    """Footprint heatmap: delta at each index price level × candle."""
    import plotly.graph_objects as go
    idx = [i for i in f.candles.tail(n_show).index if i in f.footprint]
    if not idx:
        return None
    rows = []
    for i in idx:
        fp = f.footprint[i]
        rows.append(pd.DataFrame(dict(t=f.candles.loc[i, "t"], level=fp["level"], delta=fp["delta"])))
    d = pd.concat(rows).pivot_table(index="level", columns="t", values="delta", aggfunc="sum")
    lim = float(np.nanpercentile(np.abs(d.values), 95)) if np.isfinite(d.values).any() else 1.0
    fig = go.Figure(go.Heatmap(z=d.values, x=d.columns, y=d.index, zmin=-lim, zmax=lim, zmid=0,
                               colorscale=[[0, RED], [0.5, PANEL], [1, GREEN]], colorbar=dict(title="₹ Cr", tickfont=dict(color=WHITE)),
                               hovertemplate="%{x|%H:%M} · %{y:,.0f}: delta ₹%{z:,.1f} Cr<extra></extra>"))
    c = f.candles.loc[idx]
    fig.add_trace(go.Scatter(x=c["t"], y=c["c"], mode="lines+markers", name="close", line=dict(color=WHITE, width=1)))
    return _layout(fig, "Footprint heatmap — where buyers (green) and sellers (red) were aggressive", 420)


def chart_profile(f: nf.Flow):
    import plotly.graph_objects as go
    p = f.profile
    fig = go.Figure()
    fig.add_trace(go.Bar(y=p["level"], x=p["buy"], orientation="h", name="Aggressive buy", marker_color=GREEN, opacity=0.85))
    fig.add_trace(go.Bar(y=p["level"], x=-p["sell"], orientation="h", name="Aggressive sell", marker_color=RED, opacity=0.85))
    for y, nm, col in ((f.poc, "POC", GOLD), (f.vah, "VAH", BLUE), (f.val, "VAL", BLUE)):
        if y:
            fig.add_hline(y=y, line=dict(color=col, dash="dash", width=1), annotation_text=f"{nm} {y:,.0f}",
                          annotation_font_color=col)
    fig.update_layout(barmode="relative", hovermode="y unified")
    fig.update_xaxes(title="₹ crore (sell left · buy right)")
    return _layout(fig, "Session volume profile on the NIFTY 50 price (₹ traded in the 50 stocks)", 520)


def chart_stocks(f: nf.Flow, k: int = 8):
    import plotly.graph_objects as go
    s = f.stocks
    top = pd.concat([s.head(k), s.tail(k)]).drop_duplicates("sym").sort_values("delta")
    fig = go.Figure(go.Bar(x=top["delta"], y=top["sym"], orientation="h",
                           marker_color=np.where(top["delta"] >= 0, GREEN, RED),
                           text=[f"{v:+,.0f}" for v in top["delta"]], textposition="outside",
                           hovertemplate="%{y}: net ₹%{x:,.1f} Cr<extra></extra>"))
    fig.update_xaxes(title="Net aggressive flow today (₹ crore)")
    return _layout(fig, "Which stocks are driving the flow", 460)


# ═══════════════════════════════════════════════════════════════════════════
# Footprint table (one candle)
# ═══════════════════════════════════════════════════════════════════════════
def footprint_html(f: nf.Flow, i: int) -> str:
    fp = f.footprint.get(i)
    if fp is None or fp.empty:
        return "<i>No trades in this candle.</i>"
    c = f.candles.loc[i]
    fp = fp.sort_values("level", ascending=False)
    mx = float(fp["total"].max()) or 1.0
    rows = []
    for r in fp.itertuples():
        poc = c.get("poc") == r.level
        in_va = c.get("val") is not None and c.get("vah") is not None and c["val"] <= r.level <= c["vah"]
        bg = "rgba(212,175,55,0.18)" if poc else ("rgba(56,189,248,0.07)" if in_va else "transparent")
        sb = f"border:2px solid {RED};" if r.sell_imb else ""
        bb = f"border:2px solid {GREEN};" if r.buy_imb else ""
        w = int(100 * r.total / mx)
        rows.append(
            f"<tr style='background:{bg}'><td style='color:{MUTED}'>{r.level:,.0f}{' ◀ POC' if poc else ''}</td>"
            f"<td style='text-align:right;color:{RED};{sb}'>{r.sell:,.1f}</td>"
            f"<td style='text-align:left;color:{GREEN};{bb}'>{r.buy:,.1f}</td>"
            f"<td style='text-align:right;color:{GREEN if r.delta >= 0 else RED};font-weight:700'>{r.delta:+,.1f}</td>"
            f"<td><div style='background:{BLUE};height:8px;width:{w}%;opacity:0.6;border-radius:2px'></div></td></tr>")
    ha, la = c.get("high_auction", "?"), c.get("low_auction", "?")
    return (f"<div style='background:{NAVY};border:1px solid {GRID};border-radius:8px;padding:10px 12px'>"
            f"<div style='color:{WHITE};margin-bottom:6px'>Candle <b>{c['t']:%H:%M}</b> · O {c['o']:,.1f} H {c['h']:,.1f} "
            f"L {c['l']:,.1f} C {c['c']:,.1f} · buy ₹{c['buy']:,.0f} Cr · sell ₹{c['sell']:,.0f} Cr · "
            f"<b style='color:{GREEN if c['delta'] >= 0 else RED}'>delta {_cr(c['delta'])} ({c['delta_pct']:+.0f}%)</b> · "
            f"breadth {int(c['breadth_up'])}↑/{int(c['breadth_dn'])}↓ · high auction <b>{ha}</b> · low auction <b>{la}</b></div>"
            f"<table style='width:100%;border-collapse:collapse;font-family:monospace;font-size:0.85rem;color:{WHITE}'>"
            f"<tr style='color:{GOLD}'><th style='text-align:left'>NIFTY level</th><th style='text-align:right'>Sell ₹Cr</th>"
            f"<th style='text-align:left'>Buy ₹Cr</th><th style='text-align:right'>Delta</th><th>Total</th></tr>"
            + "".join(rows) + "</table>"
            f"<div style='color:{MUTED};font-size:0.75rem;margin-top:4px'>Green box = buy imbalance (buy ≥ {nf.rules()['imbalance_ratio']:g}× the sell one level "
            f"below) · red box = sell imbalance · gold row = candle POC · blue rows = 70% value area.</div></div>")


# ═══════════════════════════════════════════════════════════════════════════
# Render
# ═══════════════════════════════════════════════════════════════════════════
def _status(hb: dict, has_today: bool) -> tuple[str, str]:
    if not hb:
        return RED, "Recorder has never run on this machine."
    age = hb.get("age_s", 1e9)
    live = hb.get("status") in ("connected",) and age < 30
    if live:
        return GREEN, (f"RECORDING · {hb.get('stocks_streaming', 0)}/{hb.get('stocks_subscribed', 0)} stocks streaming · "
                       f"{hb.get('trades', 0):,} trades classified · updated {age:.0f}s ago")
    if hb.get("error"):
        return RED, f"Recorder {hb.get('status')} — last error: {hb['error']} (updated {hb.get('updated')})"
    return (AMBER if has_today else RED), f"Recorder {hb.get('status', 'stopped')} · last update {hb.get('updated')}"


def _setup_help(st):
    st.markdown(f"""<div style="background:{PANEL};border:1px solid {GRID};border-radius:10px;padding:14px 18px;color:{WHITE}">
    <b style="color:{GOLD}">How to start recording real order flow</b><br>
    1 · Make sure <code>.env</code> has <code>DHAN_ACCESS_TOKEN</code> (fresh, they last 24 h) and <code>DHAN_CLIENT_ID</code>.<br>
    2 · Check the feed once: <code>.venv/bin/python orderflow_recorder.py --diagnose 20</code> — you should see 20 decoded packets.<br>
    3 · Record a session: <code>.venv/bin/python orderflow_recorder.py</code> (waits for 09:08, stops 15:35) — or install
        <code>nifty-orderflow.service</code> so it runs every weekday by itself.<br>
    4 · Order flow cannot be downloaded for past days — it exists only for days the recorder ran.
    </div>""", unsafe_allow_html=True)


def render(spot: float | None = None, pa_frames: dict | None = None, **_ignored) -> None:
    import streamlit as st
    st.markdown(f"""<div style="background:linear-gradient(90deg,{NAVY},{PANEL});border:1px solid {GRID};
      border-left:4px solid {GOLD};border-radius:10px;padding:12px 18px;margin-bottom:10px">
      <div style="color:{GOLD};font-weight:800;font-size:1.15rem">🌊 NIFTY 50 ORDER FLOW — who is really buying and selling</div>
      <div style="color:#cbd5e1;font-size:0.85rem;margin-top:4px">The index itself has no volume. This tab measures aggressive
      buying and selling in the <b>50 stocks that make up NIFTY 50</b> (Dhan live feed, cash market) and maps every trade onto the
      NIFTY 50 price at that moment. Real recorded data only — nothing simulated.</div></div>""", unsafe_allow_html=True)

    days = nf.recorded_days()
    hb = nf.heartbeat()
    today = date.today().isoformat()
    col, msg = _status(hb, today in days)
    st.markdown(f"<div style='border-left:4px solid {col};background:{PANEL};padding:6px 12px;color:{col};font-weight:700;"
                f"border-radius:4px;margin-bottom:8px'>● {msg}</div>", unsafe_allow_html=True)
    if not days:
        _setup_help(st)
        return

    c1, c2, c3 = st.columns([1.3, 1, 1])
    with c1:
        day = st.selectbox("Session", list(reversed(days)), index=0, key="of_day")
    with c2:
        tf = st.selectbox("Candle", [1, 3, 5, 15], index=2, format_func=lambda m: f"{m} min", key="of_tf")
    with c3:
        n_show = st.slider("Candles shown", 12, 200, 60, step=4, key="of_n")

    @st.cache_data(ttl=20, show_spinner=False)
    def _flow(d: str, m: int, mtime: float):
        return nf.compute(nf.load_day(d), m, day=d)
    fpath = nf.OF_DIR / "ticks" / f"{day}.csv"
    try:
        f = _flow(day, tf, fpath.stat().st_mtime)
    except Exception as e:                                        # noqa: BLE001
        st.error(f"Could not read the recording for {day}: {e}")
        return
    if f is None or f.candles.empty:
        st.warning(f"No classified trades inside 09:15–15:30 for {day} yet.")
        _setup_help(st)
        return
    val = nf.validation()
    R = nf.rules()
    c = f.candles
    last = c.iloc[-1]
    if f.totals["stocks"] < 40:
        st.warning(f"Only {f.totals['stocks']} of 50 stocks have trades in this recording — order flow is incomplete.")

    # ── scoreboard ───────────────────────────────────────────────────────
    st_ = f.state
    scol = GREEN if st_["code"] > 0 else RED if st_["code"] < 0 else MUTED
    bk = f.book
    cards = [
        _card("DAY CUMULATIVE DELTA", _cr(f.totals["cvd"]), f"buy ₹{f.totals['buy']:,.0f} · sell ₹{f.totals['sell']:,.0f} Cr",
              GREEN if f.totals["cvd"] >= 0 else RED),
        _card(f"LAST CANDLE {last['t']:%H:%M}", _cr(last["delta"]), f"{last['delta_pct']:+.0f}% of its ₹{last['vol']:,.0f} Cr",
              GREEN if last["delta"] >= 0 else RED),
        _card("BREADTH (LAST CANDLE)", f"{int(last['breadth_up'])}↑ / {int(last['breadth_dn'])}↓",
              "stocks net buying / net selling", GREEN if last["breadth_up"] > last["breadth_dn"] else RED),
        _card(f"PRICE vs DELTA ({st_.get('k', 0)} candles)", st_["label"].split(" — ")[0],
              (st_["label"].split(" — ")[1] if " — " in st_["label"] else ""), scol),
        _card("RESTING ORDER BOOK", f"{bk['ratio']:.2f}× bid/ask" if bk.get("ratio") else "—",
              (f"₹{bk['bid_cr']:,.0f} Cr bids vs ₹{bk['ask_cr']:,.0f} Cr offers (context only — can be pulled)" if bk else "no snapshot"),
              BLUE),
    ]
    cols = st.columns(len(cards))
    for cc, h in zip(cols, cards):
        cc.markdown(h, unsafe_allow_html=True)

    # ── verdict ──────────────────────────────────────────────────────────
    lng, lng_why = nf.confirm(f, +1, R)
    sht, sht_why = nf.confirm(f, -1, R)

    def tag(s):
        return {"CONFIRMED": GREEN, "AGAINST": RED, "NEUTRAL": MUTED, "NO DATA": MUTED}[s]
    any_valid = any(v.get("passed") for v in val.get("signals", {}).values())
    vnote = ("Some signal types passed your backtest — see the badges." if any_valid else
             "No order-flow signal has passed a backtest on your data yet — treat this as context, not a trade trigger."
             if val else "Not backtested yet — run backtest_orderflow.py after ~20 recorded sessions.")
    st.markdown(f"""<div style="background:{NAVY};border:1px solid {GRID};border-radius:10px;padding:12px 16px;margin:10px 0">
      <div style="color:{GOLD};font-weight:800">What the order flow says right now</div>
      <div style="color:{WHITE};margin-top:6px">For a <b>LONG / BUY CE</b> idea: <b style="color:{tag(lng)}">{lng}</b> — {lng_why}</div>
      <div style="color:{WHITE};margin-top:3px">For a <b>SHORT / BUY PE</b> idea: <b style="color:{tag(sht)}">{sht}</b> — {sht_why}</div>
      <div style="color:{AMBER};font-size:0.8rem;margin-top:6px">⚠ {vnote}</div></div>""", unsafe_allow_html=True)

    st.plotly_chart(chart_main(f, n_show, val), use_container_width=True, key="of_main")

    t1, t2, t3, t4, t5 = st.tabs(["🔬 Footprint", "📊 Volume profile", "🚨 Signals", "🏦 Stocks driving flow", "📖 How to read"])
    with t1:
        opts = [i for i in c.tail(n_show).index if i in f.footprint]
        lab = {i: f"{c.loc[i, 't']:%H:%M}  ·  delta {_cr(c.loc[i, 'delta'])}" for i in opts}
        pick = st.selectbox("Candle", list(reversed(opts)), format_func=lambda i: lab[i], key="of_pick")
        a, b = st.columns([1, 1.2])
        with a:
            st.markdown(footprint_html(f, pick), unsafe_allow_html=True)
        with b:
            h = chart_heat(f, min(n_show, 30))
            if h is not None:
                st.plotly_chart(h, use_container_width=True, key="of_heat")
    with t2:
        st.plotly_chart(chart_profile(f), use_container_width=True, key="of_prof")
        px = float(c["c"].iloc[-1])
        where = ("ABOVE value — buyers accepted higher prices" if f.vah and px > f.vah else
                 "BELOW value — sellers accepted lower prices" if f.val and px < f.val else "INSIDE value — balanced, mean-reversion to POC is common")
        st.markdown(f"<div style='background:{NAVY};padding:8px 12px;border-radius:6px;color:{WHITE}'>NIFTY {px:,.1f} is <b>{where}</b> · POC {f.poc:,.0f} · value area "
                    f"{f.val:,.0f}–{f.vah:,.0f} (70% of the day's ₹ traded).</div>", unsafe_allow_html=True)
    with t3:
        ev = f.events.copy()
        if ev.empty:
            st.info("No order-flow signals in this session.")
        else:
            ev["Time"] = ev["t"].dt.strftime("%H:%M")
            ev["Signal"] = ev["kind"].map(lambda k: nf.SIGNALS[k][0])
            ev["Bias"] = ev["kind"].map(lambda k: "Bullish" if k in nf.BULLISH else "Bearish")
            ev["Backtest"] = ev["kind"].map(lambda k: nf.status_of(k, val))
            ev["Detail"] = ev["detail"]
            st.dataframe(ev[["Time", "Signal", "Bias", "Detail", "Backtest"]].iloc[::-1], hide_index=True, use_container_width=True)
        rows = "".join(f"<tr><td style='color:{GOLD};padding:4px 8px'>{t_}</td><td style='padding:4px 8px'>{m}</td>"
                       f"<td style='padding:4px 8px'>{_badge(k, val)}</td></tr>" for k, (t_, m) in nf.SIGNALS.items())
        st.markdown(f"<table style='background:{NAVY};color:{WHITE};font-size:0.85rem;border-radius:6px'>{rows}</table>", unsafe_allow_html=True)
    with t4:
        st.plotly_chart(chart_stocks(f), use_container_width=True, key="of_stk")
        s = f.stocks.rename(columns={"sym": "Stock", "delta": "Net ₹Cr", "delta_15m": "Last 15m ₹Cr", "buy_pct": "Buy %",
                                     "chg_pct": "Price %", "vol": "Traded ₹Cr"})
        st.dataframe(s[["Stock", "Net ₹Cr", "Last 15m ₹Cr", "Buy %", "Price %", "Traded ₹Cr"]].round(2),
                     hide_index=True, use_container_width=True)
    with t5:
        st.markdown(f"""<div style="background:{NAVY};padding:12px 16px;border-radius:8px;color:{WHITE};font-size:0.9rem;line-height:1.55">
        <b style="color:{GOLD}">Delta</b> = aggressive buying − aggressive selling (₹ crore). A buyer who lifts the offer is aggressive;
        so is a seller who hits the bid. Positive = buyers pushing.<br>
        <b style="color:{GOLD}">Cumulative delta (CVD)</b> = delta added up since 09:15 — who has dominated the day.<br>
        <b style="color:{GOLD}">Breadth</b> = how many of the 50 stocks were net buyers in the candle. A move driven by 40 stocks is
        sturdier than one driven by HDFC Bank alone.<br>
        <b style="color:{GOLD}">The 4 rules</b> — price ↑ + delta ↑ = strong bullish · price ↓ + delta ↓ = strong bearish ·
        price ↑ + delta ↓ = bearish divergence / absorption · price ↓ + delta ↑ = bullish divergence.<br>
        <b style="color:{GOLD}">Footprint</b> = buy × sell traded at each NIFTY price level inside one candle; the gold row (POC) is
        where most money changed hands.<br>
        <b style="color:{GOLD}">Imbalance</b> = buy at a level ≥ {R['imbalance_ratio']:g}× the sell one level below (or the reverse).
        <b>Stacked</b> = {R['stack_min']}+ in a row → a zone that often acts as support (buy) or resistance (sell) until a candle
        closes through it.<br>
        <b style="color:{GOLD}">Absorption</b> = a big delta (≥ {R['absorption_min_delta_pct']:g}% of the candle) with almost no price
        progress — the passive side won.<br>
        <b style="color:{GOLD}">Use it as the execution layer</b>: direction from structure (15m), options flow and levels; order flow
        only confirms or vetoes the entry. Signals stay <b>UNTESTED</b> until <code>backtest_orderflow.py</code> beats random
        times on your recorded sessions.<br>
        <b style="color:{GOLD}">Limits</b>: Dhan sends snapshots, not every exchange print, and NSE does not publish the aggressor
        side — it is inferred (quote rule, then tick rule), as on every Indian order-flow platform. Information only — not advice.
        </div>""", unsafe_allow_html=True)
