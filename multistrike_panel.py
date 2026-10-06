"""
multistrike_panel.py — Multi-strike OI (like StockMojo's "Multi OI & Volume" / "MultiStrike OI Change"), a sub-tab of 🧪 Options Lab.

For the strikes you pick (default: the 5 call/put strikes with the most OI right now) it draws, through the day:
  · Total OI per strike          (is the wall being built, held or abandoned?)
  · OI change since yesterday    (fresh writing vs unwinding at each strike)
with NIFTY on the right axis, and a Call-vs-Put summary of the picked strikes.
Hover any line → it names the strike and side ("22,700 PE"), the time, the OI and its change.

Reads the stored snapshots only (store.load_day). OI unit = options_lab.oi_multiplier (1 = lots, 65 = quantity like StockMojo).
Information only — not financial advice.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

import options_lab_tab as ol
from options_lab_tab import fmt, NAVY, GOLD, WHITE, MUTED, GREEN, RED, FUT

PALETTE = ["#3b82f6", "#22c55e", "#eab308", "#f97316", "#ec4899", "#a855f7", "#14b8a6", "#ef4444",
           "#84cc16", "#06b6d4", "#f43f5e", "#8b5cf6"]


# ═══════════════════════════════════════════════════════════════════════════
# 1 · Data
# ═══════════════════════════════════════════════════════════════════════════
def legs_frame(snaps: list, mult: float) -> pd.DataFrame:
    """Long table: one row per (snapshot, strike, side) with oi, chg (vs yesterday's close), vol."""
    rows = []
    for ts, spot, df in snaps:
        if df is None or df.empty:
            continue
        d = ol._one_expiry(df)
        num = lambda c: pd.to_numeric(d[c], errors="coerce") if c in d else pd.Series(float("nan"), index=d.index)
        k = pd.to_numeric(d["strike"], errors="coerce")
        for side, p in (("CE", "ce"), ("PE", "pe")):
            part = pd.DataFrame({"ts": pd.Timestamp(ts), "spot": float(spot), "strike": k, "side": side,
                                 "oi": num(f"{p}_oi") * mult, "chg": num(f"{p}_chg_oi") * mult, "vol": num(f"{p}_vol")})
            rows.append(part)
    if not rows:
        return pd.DataFrame(columns=["ts", "spot", "strike", "side", "oi", "chg", "vol"])
    out = pd.concat(rows, ignore_index=True).dropna(subset=["strike"])
    out["strike"] = out["strike"].round().astype(int)
    if out["chg"].isna().all():                     # older snapshots without a change column → change vs first snapshot
        first = out.sort_values("ts").groupby(["strike", "side"])["oi"].transform("first")
        out["chg"] = out["oi"] - first
    return out


def pick_legs(lf: pd.DataFrame, how: str, n: int) -> list[tuple[int, str]]:
    """Top-n (strike, side) by latest OI ('High OI') or by latest day volume ('High Volume')."""
    last = lf[lf["ts"] == lf["ts"].max()]
    col = "oi" if how == "High OI" else "vol"
    top = last.sort_values(col, ascending=False).head(int(n))
    return [(int(r.strike), r.side) for r in top.itertuples()]


def resample(lf: pd.DataFrame, legs: list, minutes: int) -> pd.DataFrame:
    """Wide-ish frame per leg on the chosen candle (value = last snapshot in the candle)."""
    want = set(legs)
    keep = lf[[(int(s), sd) in want for s, sd in zip(lf["strike"], lf["side"])]] if len(lf) else lf
    if keep.empty:
        return keep
    parts = []
    for (k, sd), g in keep.groupby(["strike", "side"]):
        x = g.set_index("ts").sort_index()
        if minutes > 1:
            x = x.resample(f"{minutes}min", origin="start_day", offset="15min").last().dropna(subset=["oi"])
        x = x.reset_index()
        x["strike"], x["side"] = k, sd
        parts.append(x)
    return pd.concat(parts, ignore_index=True)


# ═══════════════════════════════════════════════════════════════════════════
# 2 · Charts
# ═══════════════════════════════════════════════════════════════════════════
def _label(k: int, sd: str) -> str:
    return f"{k:,} {sd}"


def chart_lines(w: pd.DataFrame, legs: list, value: str, title: str, hover_all: bool) -> go.Figure:
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    sp = w.drop_duplicates("ts").sort_values("ts")
    fig.add_trace(go.Scatter(x=sp["ts"], y=sp["spot"], name="NIFTY", line=dict(color=FUT, dash="dot", width=1.3),
                             hovertemplate="NIFTY %{y:,.1f}<extra></extra>"), secondary_y=True)
    for i, (k, sd) in enumerate(legs):
        g = w[(w["strike"] == k) & (w["side"] == sd)].sort_values("ts")
        if g.empty:
            continue
        name = _label(k, sd)
        cd = list(zip([fmt(v).lstrip("+") for v in g["oi"]], [fmt(v) for v in g["chg"]]))
        fig.add_trace(go.Scatter(
            x=g["ts"], y=g[value], name=name, mode="lines",
            line=dict(color=PALETTE[i % len(PALETTE)], width=2.4, dash="solid" if sd == "PE" else "dash"),
            customdata=cd,
            hovertemplate=f"<b>{name}</b> · %{{x|%H:%M}}<br>OI %{{customdata[0]}} · change vs yesterday %{{customdata[1]}}<extra></extra>"),
            secondary_y=False)
    ol._layout(fig, title, h=520)
    fig.update_layout(hovermode="x unified" if hover_all else "closest",
                      hoverlabel=dict(bgcolor=NAVY, font=dict(color=WHITE, size=12), bordercolor=GOLD))
    fig.update_yaxes(title_text="OI" if value == "oi" else "OI change (vs yesterday)", secondary_y=False)
    fig.update_yaxes(title_text="NIFTY", secondary_y=True, showgrid=False)
    ys = w[value].dropna()
    if len(ys):
        import numpy as np
        ticks = np.linspace(float(ys.min()), float(ys.max()), 6)
        fig.update_yaxes(tickvals=ticks, ticktext=[fmt(v).lstrip("+") if value == "oi" else fmt(v) for v in ticks], secondary_y=False)
    return fig


def chart_summary(last: pd.DataFrame) -> go.Figure:
    """Call vs Put totals of the picked strikes at the latest snapshot (like StockMojo's two bar boxes)."""
    ce, pe = last[last["side"] == "CE"], last[last["side"] == "PE"]
    fig = make_subplots(rows=1, cols=2, subplot_titles=("OI change (picked strikes)", "Total OI (picked strikes)"))
    for col, field in ((1, "chg"), (2, "oi")):
        vals = [float(ce[field].sum()), float(pe[field].sum())]
        fig.add_trace(go.Bar(x=["CALL", "PUT"], y=vals, marker_color=[GREEN, RED], text=[fmt(v) for v in vals],
                             textposition="outside", hovertemplate="%{x}: %{text}<extra></extra>", showlegend=False), 1, col)
    ol._layout(fig, "Call vs Put — the strikes shown above", h=320)
    fig.update_yaxes(showticklabels=False)
    return fig


GUIDE = ("Each line is ONE strike — solid = PUT, dashed = CALL; hover a line to see which strike, the time, its OI and change. "
         "Dotted grey = NIFTY (right axis).",
         ["A PUT line RISING while price sits above it = writers defending that floor",
          "A PUT line FALLING as price comes down to it = floor being abandoned → break more likely",
          "A CALL line RISING as price approaches = ceiling being built → breakout harder",
          "A new strike's line climbing fast = the wall is shifting (writers rolling their level)",
          "On EXPIRY day, everything falls after ~14:00 as positions close — not a direction",
          "Context, not a signal: OI readings alone showed no edge in our backtests"])


# ═══════════════════════════════════════════════════════════════════════════
# 3 · Streamlit
# ═══════════════════════════════════════════════════════════════════════════
def render(day: date | None = None) -> None:
    import streamlit as st
    import store

    days = ol._days_available()
    if not days:
        st.info("No option-chain snapshots stored yet.")
        return
    c = st.columns([1.6, 1.4, 1.6, 1.0, 1.6])
    d0 = days.index(day) if day in days else 0
    day = c[0].selectbox("Session", days, index=d0, format_func=lambda d: d.strftime("%a %d %b %Y"), key="ms_day")
    tf = c[1].radio("Candle", ["1m", "3m", "5m", "15m"], index=0, horizontal=True, key="ms_tf")
    how = c[2].radio("Pick strikes by", ["High OI", "High Volume", "Custom"], horizontal=True, key="ms_how")
    n = c[3].selectbox("How many", [3, 4, 5, 6, 8, 10], index=2, key="ms_n", disabled=(how == "Custom"))
    hover_all = c[4].radio("Hover shows", ["All lines at that time", "Only the line I point to"], index=0,
                           horizontal=False, key="ms_hover") == "All lines at that time"

    mult = float(ol._rules()["oi_multiplier"])

    @st.cache_data(ttl=60, show_spinner=False)
    def _load(d: date, n_files: int, m: float):
        return legs_frame(store.load_day(d), m)

    from pathlib import Path
    import config
    n_files = len(list((Path(config.SNAPSHOT_DIR) / day.strftime("%Y-%m-%d")).glob("*.meta.json")))
    with st.spinner("Reading snapshots…"):
        lf = _load(day, n_files, mult)
    if lf.empty or lf["ts"].nunique() < 2:
        st.warning("Not enough snapshots for this session yet.")
        return

    last = lf[lf["ts"] == lf["ts"].max()]
    spot = float(last["spot"].iloc[0])
    if how == "Custom":
        opts = [_label(int(k), sd) for k, sd in sorted(set(zip(last["strike"], last["side"])), key=lambda x: (abs(x[0] - spot), x[1]))]
        default = [_label(k, sd) for k, sd in pick_legs(lf, "High OI", 5)]
        chosen = st.multiselect("Strikes", opts, default=default, key="ms_custom")
        legs = [(int(x.split()[0].replace(",", "")), x.split()[1]) for x in chosen]
    else:
        legs = pick_legs(lf, how, n)
    if not legs:
        st.info("Pick at least one strike.")
        return
    legs = sorted(legs, key=lambda x: (x[1] != "CE", x[0]))       # calls first, then puts by strike
    st.caption("Showing: " + " · ".join(_label(k, sd) for k, sd in legs) +
               f"  ·  OI ×{mult:g} ({'quantity' if mult > 1 else 'lots'})  ·  {lf['ts'].min():%H:%M}–{lf['ts'].max():%H:%M}")

    w = resample(lf, legs, int(tf[:-1]))
    st.plotly_chart(chart_lines(w, legs, "oi", "Multi-strike OI — total open interest per strike", hover_all),
                    use_container_width=True, key="ms_oi")
    st.plotly_chart(chart_lines(w, legs, "chg", "Multi-strike OI change — vs yesterday's close", hover_all),
                    use_container_width=True, key="ms_chg")
    pick_last = last[[(int(k), sd) in set(legs) for k, sd in zip(last["strike"], last["side"])]]
    st.plotly_chart(chart_summary(pick_last), use_container_width=True, key="ms_sum")

    head, bullets = GUIDE
    items = "".join(f"<li>{b}</li>" for b in bullets)
    st.markdown(f"""<div style="background:{NAVY};border:1px solid {GOLD};border-radius:8px;padding:12px 16px">
        <div style="color:{GOLD};font-weight:700">HOW TO READ</div>
        <div style="color:{WHITE};margin:4px 0">{head}</div>
        <ul style="color:{MUTED};margin:4px 0 0 18px">{items}</ul></div>""", unsafe_allow_html=True)
