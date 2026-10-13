"""
smart_oi_pro.py — "Smart OI" dashboard for 🧪 Options Lab, laid out like StockMojo's Smart OI page and extended:

  LEFT  · NIFTY candles (real 1-minute candles when stored, else snapshot spot) with DIVERGENCE MARKERS
          ▲ green = dip defended (puts written while the candle fell)  ▼ red = rally sold (calls written while it rose)
        · Smart OI bars per candle (PE − CE OI change) + a CUMULATIVE Smart OI line (who has been winning since 09:15)
        · Call vs Put volume per candle
  RIGHT · Put OI (chg day) · Call OI (chg day) · PE − CE (chg day) with zero-cross markers (control changing hands)
        · OI PCR vs Volume PCR with 0.7 / 1.0 reference lines
  BELOW · a plain-English read of each chart from today's numbers + how to use it

Data: same calculation as the PE − CE OI Table (pe_ce_panel.per_snapshot / table_rows), so every number matches that table.
Unit = options_lab.oi_multiplier (65 = quantity like StockMojo). Information only — not financial advice.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

import options_lab_tab as ol
import pe_ce_panel as pc
from options_lab_tab import fmt, NAVY, GOLD, WHITE, MUTED, GREEN, RED, BLUE, ORANGE, PURPLE, FUT

TF = {"1m": 1, "3m": 3, "5m": 5, "15m": 15}


# ═══════════════════════════════════════════════════════════════════════════
# 1 · Data
# ═══════════════════════════════════════════════════════════════════════════
def real_candles(day: date, minutes: int) -> pd.DataFrame | None:
    """Stored 1-minute NIFTY candles for the day, resampled; None when not stored."""
    try:
        import smc_brief
        one = smc_brief._read_1m(1, day)
        if one is None or one.empty:
            return None
        one = one[one["timestamp"].dt.date == day]
        if one.empty:
            return None
        x = one.set_index("timestamp").sort_index()
        if minutes > 1:
            x = x.resample(f"{minutes}min", origin="start_day", offset="15min").agg(
                {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
        return x.reset_index().rename(columns={"timestamp": "ts"})[["ts", "open", "high", "low", "close"]]
    except Exception:                                           # noqa: BLE001
        return None


def build(snaps: list, day: date, tf: str, mode: str, n: int | None, r: dict) -> tuple[pd.DataFrame, pd.DataFrame, str]:
    win = pc.strike_window(snaps, mode, None if mode == "All" else n, None, r["strike_step"])
    s = pc.per_snapshot(snaps, win, r["oi_multiplier"])
    if s.empty:
        return s, s, ""
    t = pc.table_rows(s, TF[tf])
    t["cum_smart"] = t["smart_oi"].cumsum()                     # first bar = its day change → running total = PE − CE (day)
    cd = real_candles(day, TF[tf])
    if cd is not None and len(cd) >= 3:
        cd = cd[(cd["ts"] >= t["ts"].min()) & (cd["ts"] <= t["ts"].max())]
    real = cd is not None and len(cd) >= 3
    candles = cd if real else t[["ts", "open", "high", "low", "close"]].copy()
    candles.attrs["real"] = real
    rng = f"{win[0]:,.0f} – {win[1]:,.0f}" if win else "all strikes"
    return t, candles, rng


def divergences(t: pd.DataFrame, candles: pd.DataFrame, pct: float = 0.75) -> pd.DataFrame:
    """Candles where writers acted AGAINST the candle: big + Smart OI on a red candle, big − Smart OI on a green one."""
    if t.empty:
        return t
    m = t[["ts", "smart_oi"]].merge(candles, on="ts", how="inner")
    if m.empty:
        return m
    thr = float(m["smart_oi"].abs().quantile(pct)) if len(m) > 4 else float(m["smart_oi"].abs().max())
    red = m["close"] < m["open"]
    m["kind"] = np.where((m["smart_oi"] >= thr) & red, "defended",
                         np.where((m["smart_oi"] <= -thr) & ~red, "sold", ""))
    # FIX-3: "rally sold" inside a confirmed resistance break (or "dip defended" inside a support break) is writers
    # exiting/adjusting, not capping — and expiry-afternoon call OI changes are mostly exits. Drop those markers.
    if "lv_state" in t:
        stt = t[["ts", "lv_state"]].merge(m[["ts"]], on="ts", how="right")["lv_state"].fillna("").values
        m.loc[(m["kind"] == "sold") & (stt == "UP"), "kind"] = ""
        m.loc[(m["kind"] == "defended") & (stt == "DN"), "kind"] = ""
    if "exp_day" in t:
        ex = t[["ts", "exp_day"]].merge(m[["ts"]], on="ts", how="right")["exp_day"].fillna(False).astype(bool).values
        late = m["ts"].dt.time >= __import__("datetime").time(14, 30)
        m.loc[ex & late.values, "kind"] = ""
    return m[m["kind"] != ""]


def crossings(t: pd.DataFrame, frac: float = 0.10) -> pd.DataFrame:
    """PE − CE changing sign — counted only when it moves clearly past zero (10 % of the day's largest gap), so noise near 0 is ignored."""
    v = t["pe_ce_day"].astype(float).values
    band = frac * float(np.nanmax(np.abs(v))) if len(v) else 0.0
    idx, side = [], 0
    for i, x in enumerate(v):
        now = 1 if x > band else -1 if x < -band else 0
        if now and side and now != side:
            idx.append(i)
        if now:
            side = now
    return t.iloc[idx]


# ═══════════════════════════════════════════════════════════════════════════
# 2 · Charts
# ═══════════════════════════════════════════════════════════════════════════
def _inr_ticks(fig, vals, **sel):
    v = pd.Series(vals, dtype=float).dropna()
    if v.empty:
        return
    lo, hi = float(v.min()), float(v.max())
    if hi == lo:
        hi = lo + 1
    ticks = np.linspace(lo, hi, 6)
    fig.update_yaxes(tickvals=ticks, ticktext=[fmt(x) for x in ticks], **sel)


def align_zero(fig, left, right, row=1, col=1):
    """FIX-7: put 0 at the same height on both y-axes so lines only 'cross' where they really do."""
    a, b = pd.Series(left, dtype=float).dropna(), pd.Series(right, dtype=float).dropna()
    if a.empty or b.empty:
        return
    la, ha = min(float(a.min()), 0.0), max(float(a.max()), 0.0)
    lb, hb = min(float(b.min()), 0.0), max(float(b.max()), 0.0)
    fa = -la / (ha - la) if ha > la else 0.5                   # where zero sits (0 = bottom, 1 = top)
    fb = -lb / (hb - lb) if hb > lb else 0.5
    f = max(fa, fb)
    f = min(max(f, 0.05), 0.95)
    def rng(lo, hi):
        span = max(hi / (1 - f) if hi > 0 else 0, -lo / f if lo < 0 else 0) or 1.0
        return [-f * span * 1.05, (1 - f) * span * 1.05]
    ra, rb = rng(la, ha), rng(lb, hb)
    ta, tb = np.linspace(ra[0], ra[1], 6), np.linspace(rb[0], rb[1], 6)
    fig.update_yaxes(range=ra, tickvals=ta, ticktext=[fmt(x) for x in ta], row=row, col=col, secondary_y=False)
    fig.update_yaxes(range=rb, tickvals=tb, ticktext=[fmt(x) for x in tb], row=row, col=col, secondary_y=True)


def _legend_below(fig: go.Figure) -> go.Figure:
    fig.update_layout(legend=dict(orientation="h", x=0, xanchor="left", y=-0.06, yanchor="top", font=dict(size=11)),
                      margin=dict(l=10, r=10, t=60, b=80), hovermode="x unified")
    return fig


def chart_left(t: pd.DataFrame, candles: pd.DataFrame, dv: pd.DataFrame, tf: str) -> go.Figure:
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, row_heights=[0.52, 0.26, 0.22], vertical_spacing=0.03,
                        specs=[[{}], [{"secondary_y": True}], [{}]])
    fig.add_trace(go.Candlestick(x=candles["ts"], open=candles["open"], high=candles["high"], low=candles["low"],
                                 close=candles["close"], name="NIFTY", increasing_line_color=GREEN, decreasing_line_color=RED,
                                 increasing_fillcolor=GREEN, decreasing_fillcolor=RED), 1, 1)
    if len(dv):
        d1, d2 = dv[dv["kind"] == "defended"], dv[dv["kind"] == "sold"]
        fig.add_trace(go.Scatter(x=d1["ts"], y=d1["low"] - 4, mode="markers", name="Dip defended ▲",
                                 marker=dict(symbol="triangle-up", size=11, color=GREEN, line=dict(color=WHITE, width=1)),
                                 customdata=[fmt(v) for v in d1["smart_oi"]],
                                 hovertemplate="Dip defended ▲ — puts written into a red candle (Smart OI %{customdata})<extra></extra>"), 1, 1)
        fig.add_trace(go.Scatter(x=d2["ts"], y=d2["high"] + 4, mode="markers", name="Rally sold ▼",
                                 marker=dict(symbol="triangle-down", size=11, color=RED, line=dict(color=WHITE, width=1)),
                                 customdata=[fmt(v) for v in d2["smart_oi"]],
                                 hovertemplate="Rally sold ▼ — calls written into a green candle (Smart OI %{customdata})<extra></extra>"), 1, 1)
    cols = ["rgba(34,197,94,0.8)" if v >= 0 else "rgba(239,68,68,0.8)" for v in t["smart_oi"]]
    fig.add_trace(go.Bar(x=t["ts"], y=t["smart_oi"], name="Smart OI (PE − CE per candle)", marker_color=cols,
                         customdata=[fmt(v) for v in t["smart_oi"]], hovertemplate="Smart OI %{customdata}<extra></extra>"),
                  2, 1, secondary_y=False)
    fig.add_trace(go.Scatter(x=t["ts"], y=t["cum_smart"], name="Cumulative Smart OI (= PE − CE day)",
                             line=dict(color=GOLD, width=2), customdata=[fmt(v) for v in t["cum_smart"]],
                             hovertemplate="Cumulative %{customdata}<extra></extra>"), 2, 1, secondary_y=True)
    fig.add_trace(go.Bar(x=t["ts"], y=t["call_vol"], name="Call volume", marker_color=GREEN,
                         customdata=[fmt(v) for v in t["call_vol"]], hovertemplate="Call vol %{customdata}<extra></extra>"), 3, 1)
    fig.add_trace(go.Bar(x=t["ts"], y=t["put_vol"], name="Put volume", marker_color=RED,
                         customdata=[fmt(v) for v in t["put_vol"]], hovertemplate="Put vol %{customdata}<extra></extra>"), 3, 1)
    fig.update_layout(xaxis_rangeslider_visible=False, barmode="group", bargap=0.12)
    fig.update_yaxes(title_text="NIFTY", row=1, col=1)
    fig.update_yaxes(title_text="Smart OI", row=2, col=1, secondary_y=False)
    fig.update_yaxes(title_text="Cumulative", row=2, col=1, secondary_y=True, showgrid=False)
    fig.update_yaxes(title_text="Volume", row=3, col=1)
    _inr_ticks(fig, t["smart_oi"], row=2, col=1, secondary_y=False)
    _inr_ticks(fig, t["cum_smart"], row=2, col=1, secondary_y=True)
    _inr_ticks(fig, pd.concat([t["call_vol"], t["put_vol"]]), row=3, col=1)
    pc.clip_volume(fig, pd.concat([t["call_vol"], t["put_vol"]]), row=3, col=1)           # FIX-6
    align_zero(fig, t["smart_oi"], t["cum_smart"], row=2, col=1)                           # FIX-7
    pc.add_levels(fig, t, row=1, col=1)                                                    # LEVELS
    return _legend_below(ol._layout(fig, f"NIFTY · Smart OI · Volume ({tf})", h=800))


def chart_right(t: pd.DataFrame, cr: pd.DataFrame) -> go.Figure:
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.55, 0.45], vertical_spacing=0.08,
                        specs=[[{"secondary_y": True}], [{}]],
                        subplot_titles=("Put OI · Call OI · PE − CE (change since yesterday)", "OI PCR vs Volume PCR"))
    for col, name, color, sec in (("pe_day", "Put OI (chg day)", RED, False), ("ce_day", "Call OI (chg day)", GREEN, False),
                                  ("pe_ce_day", "PE − CE (chg day)", PURPLE, True)):
        fig.add_trace(go.Scatter(x=t["ts"], y=t[col], name=name, line=dict(color=color, width=2.4),
                                 customdata=[fmt(v) for v in t[col]], hovertemplate=f"{name} %{{customdata}}<extra></extra>"),
                      1, 1, secondary_y=sec)
    fig.add_hline(y=0, line=dict(color=MUTED, width=1, dash="dot"), row=1, col=1, secondary_y=True)
    if len(cr):
        fig.add_trace(go.Scatter(x=cr["ts"], y=cr["pe_ce_day"], mode="markers", name="PE − CE crossed 0",
                                 marker=dict(symbol="x", size=10, color=GOLD),
                                 hovertemplate="PE − CE crossed zero — control changed hands<extra></extra>"), 1, 1, secondary_y=True)
    fig.add_trace(go.Scatter(x=t["ts"], y=t["pcr"], name="OI PCR", line=dict(color=BLUE, width=2.4),
                             hovertemplate="OI PCR %{y:.2f}<extra></extra>"), 2, 1)
    fig.add_trace(go.Scatter(x=t["ts"], y=t["vol_pcr"], name="Vol PCR (total)", line=dict(color=ORANGE, width=2.4),
                             hovertemplate="Vol PCR %{y:.2f}<extra></extra>"), 2, 1)
    for y, txt in ((1.0, "1.0 put-heavy above"), (0.7, "0.7 call-heavy below")):
        fig.add_hline(y=y, line=dict(color=MUTED, width=1, dash="dot"), row=2, col=1,
                      annotation_text=txt, annotation_font_color=MUTED, annotation_position="top left")
    fig.update_yaxes(title_text="Put / Call OI chg", row=1, col=1, secondary_y=False)
    fig.update_yaxes(title_text="PE − CE", row=1, col=1, secondary_y=True, showgrid=False)
    fig.update_yaxes(title_text="PCR", row=2, col=1)
    _inr_ticks(fig, pd.concat([t["pe_day"], t["ce_day"]]), row=1, col=1, secondary_y=False)
    _inr_ticks(fig, t["pe_ce_day"], row=1, col=1, secondary_y=True)
    align_zero(fig, pd.concat([t["pe_day"], t["ce_day"]]), t["pe_ce_day"], row=1, col=1)     # FIX-7
    for a in fig.layout.annotations:
        a.font = dict(color=GOLD, size=13)
    return _legend_below(ol._layout(fig, "Writers & PCR", h=800))


# ═══════════════════════════════════════════════════════════════════════════
# 3 · Plain-English reads (from today's numbers) + how to use
# ═══════════════════════════════════════════════════════════════════════════
def reads(t: pd.DataFrame, dv: pd.DataFrame, cr: pd.DataFrame, last_n: int = 10) -> dict:
    tail = t.tail(last_n)
    sm = float(tail["smart_oi"].sum())
    px = float(tail["close"].iloc[-1] - tail["open"].iloc[0])
    grn, red = int((tail["smart_oi"] > 0).sum()), int((tail["smart_oi"] < 0).sum())
    nd, ns = int((dv["kind"] == "defended").sum()) if len(dv) else 0, int((dv["kind"] == "sold").sum()) if len(dv) else 0
    agree = (sm > 0 and px > 0) or (sm < 0 and px < 0)
    smart = (f"Last {len(tail)} candles: Smart OI {fmt(sm)} ({grn} green / {red} red bars) while NIFTY moved {px:+.0f} pts — "
             + ("writers AGREE with price." if agree else "writers are going AGAINST price — watch for a stall/turn.")
             + f" Today so far: {nd} dip-defended ▲ and {ns} rally-sold ▼ candles.")
    pe, ce, d = float(t["pe_day"].iloc[-1]), float(t["ce_day"].iloc[-1]), float(t["pe_ce_day"].iloc[-1])
    d30 = float(t["pe_ce_day"].iloc[-1] - t["pe_ce_day"].iloc[max(0, len(t) - 11)])
    lead = "put writers lead (bullish lean)" if d > 0 else "call writers lead (bearish lean)" if d < 0 else "balanced"
    oi = (f"Since yesterday: puts {fmt(pe)}, calls {fmt(ce)} → PE − CE {fmt(d)}, {lead}. "
          f"Last ~10 candles PE − CE moved {fmt(d30)} → " + ("gap widening for the leader." if d * d30 > 0 else "gap narrowing — leader losing grip.")
          + (f" Control changed hands {len(cr)}× today (gold ✕)." if len(cr) else ""))
    p0, p1, v1 = float(t["pcr"].iloc[0]), float(t["pcr"].iloc[-1]), float(t["vol_pcr"].iloc[-1])
    zone = "put-heavy, above 1" if p1 >= 1 else "call-heavy, below 0.7" if p1 < 0.7 else "middle zone"
    pcr = (f"OI PCR {p1:.2f} ({zone}), {p1 - p0:+.2f} since the open. Volume PCR {v1:.2f} — "
           + ("today's traders are buying more puts than the positions held suggest (hedging / fear; often near short-term lows)."
              if v1 > p1 else "today's trading is less defensive than the positions held."))
    return dict(smart=smart, oi=oi, pcr=pcr)


HOW = {
    "smart": ("How to use the left chart",
              ["Each bar = put OI change − call OI change inside that candle (who wrote more options in that candle).",
               "Green bars under green candles / red under red = writers support the move → trend has backing.",
               "▲ Dip defended: price fell but put writers sold MORE puts — they are betting the low holds. Best near a support / floor strike.",
               "▼ Rally sold: price rose but call writers sold MORE calls — they are capping the move. Best near a resistance / ceiling strike.",
               "Gold line = running total since 09:15 (same as PE − CE day). Its SLOPE tells who is winning right now; flat = nobody.",
               "Volume spike with a big Smart OI bar = writers acting; spike with a tiny bar = traders churning, ignore."]),
    "oi": ("How to use Put / Call / PE − CE",
           ["Red (puts) rising = floors being built under price. Green (calls) rising = ceilings being built above.",
            "Purple (PE − CE, right axis) is the tug-of-war: above 0 = put-heavy, below 0 = call-heavy.",
            "Gold ✕ = PE − CE crossed zero → control changed hands. A cross with price breaking a level is stronger than one in a range.",
            "Both red and green FALLING together = positions closing (expiry-day afternoon, EOD) — not a direction."]),
    "pcr": ("How to use OI PCR vs Volume PCR",
            ["OI PCR (blue) = positions held. Above 1 = put-heavy (writers expect support). Below 0.7 = call-heavy.",
             "The CHANGE matters more than the level: PCR rising with price = puts written into the rally (healthy).",
             "Volume PCR (orange) = today's trading. Far above OI PCR = heavy put BUYING / hedging — fear, often near short-term lows.",
             "PCR alone showed no edge in our backtests — use it to confirm what the left chart shows, not as a trigger."]),
}


def _box(title_read: str, read: str, how: tuple) -> str:
    head, items = how
    li = "".join(f"<li>{x}</li>" for x in items)
    return (f"<div style='background:{NAVY};border:1px solid {GOLD};border-radius:8px;padding:10px 14px;margin:6px 0'>"
            f"<div style='color:{GOLD};font-weight:700'>{title_read}</div><div style='color:{WHITE};margin:4px 0 8px'>{read}</div>"
            f"<div style='color:{GOLD};font-weight:600'>{head}</div><ul style='color:{MUTED};margin:4px 0 0 18px'>{li}</ul></div>")


# ═══════════════════════════════════════════════════════════════════════════
# 4 · Streamlit
# ═══════════════════════════════════════════════════════════════════════════
def render(day: date | None = None) -> None:
    import streamlit as st
    import store
    import config

    r = pc._rules()
    days = ol._days_available()
    if not days:
        st.info("No option-chain snapshots stored yet.")
        return
    c = st.columns([1.6, 1.8, 1.8, 1.2])
    day = c[0].selectbox("Session", days, index=days.index(day) if day in days else 0,
                         format_func=lambda d: d.strftime("%a %d %b %Y"), key="sop_day")
    tf = c[1].radio("Candle", list(TF), index=1, horizontal=True, key="sop_tf")
    mode = c[2].radio("Strikes", ["Auto ATM", "All"], index=1, horizontal=True, key="sop_mode")
    n = c[3].selectbox("± Strikes", [5, 10, 15, 20], index=1, key="sop_n", disabled=(mode == "All"))

    @st.cache_data(ttl=60, show_spinner=False)
    def _snaps(d: date, n_files: int):
        return store.load_day(d)

    n_files = len(list((Path(config.SNAPSHOT_DIR) / day.strftime("%Y-%m-%d")).glob("*.meta.json")))
    snaps = _snaps(day, n_files)
    if len(snaps) < 3:
        st.warning("Not enough snapshots for this session yet.")
        return
    t, candles, rng = build(snaps, day, tf, mode, n, r)
    if t.empty:
        st.warning("No strikes inside that range.")
        return
    dv, cr = divergences(t, candles), crossings(t)
    st.caption(f"{len(t)} candles · strikes: {rng} · OI ×{r['oi_multiplier']:g} · "
               f"candles: {'stored 1-minute NIFTY' if candles.attrs.get('real') else 'snapshot spot'} · "
               "same numbers as the PE − CE OI Table")

    left, right = st.columns([1.25, 1])
    left.plotly_chart(chart_left(t, candles, dv, tf), use_container_width=True, key="sop_left")
    right.plotly_chart(chart_right(t, cr), use_container_width=True, key="sop_right")

    rd = reads(t, dv, cr)
    a, b, c3 = st.columns(3)
    a.markdown(_box("SMART OI — today", rd["smart"], HOW["smart"]), unsafe_allow_html=True)
    b.markdown(_box("PUT / CALL / PE − CE — today", rd["oi"], HOW["oi"]), unsafe_allow_html=True)
    c3.markdown(_box("PCR — today", rd["pcr"], HOW["pcr"]), unsafe_allow_html=True)
    st.caption("Information only — not a trading signal. OI readings alone showed no edge in our backtests; "
               "use these to understand who is positioned where, then let price at a level decide.")
