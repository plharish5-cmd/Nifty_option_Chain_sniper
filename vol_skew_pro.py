"""
vol_skew_pro.py — "😨 Volatility Skew" sub-tab of 🧪 Options Lab (StockMojo's Volatility Skew, plus a pro read).

  Chart   IV smile by strike (OTM side = the market's real price of protection: puts below spot, calls above),
          optional call-IV / put-IV curves · Call OI / Put OI bars (right axis) · spot line · lowest-IV strike ·
          previous sessions' smiles (T-1 … T-5) · time replay (any minute of the session) · X axis by strike or ATM ±.
          "Vol Ratio": put IV ÷ call IV at each strike (above 1 = puts priced richer at that strike).
  Read    ATM IV now vs open vs previous session · 25-delta RISK REVERSAL (put IV − call IV at |Δ| ≈ 0.25) and its change
          · put-wing / call-wing steepness · where the smile's bottom sits vs spot →
          FEAR BUILDING / FEAR FADING / CALLS BID / CALM, and what that means for an option BUYER.
Information only — not financial advice. Not backtested in this app.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

import options_lab_tab as ol
from options_lab_tab import NAVY, GOLD, WHITE, MUTED, GREEN, RED

BLUE, ORANGE = "#3b82f6", "#f97316"
PREV_COLS = ["#94a3b8", "#a78bfa", "#f472b6", "#2dd4bf", "#facc15"]


def frame(df: pd.DataFrame, spot: float, expiry: str | None = None) -> pd.DataFrame:
    d = df[df["expiry"].astype(str) == str(expiry)] if (expiry and "expiry" in df) else ol._one_expiry(df)
    d = d.drop_duplicates("strike").copy()
    g = lambda c: pd.to_numeric(d[c], errors="coerce") if c in d else pd.Series(np.nan, index=d.index)
    x = pd.DataFrame({"strike": g("strike"), "ce_iv": g("ce_iv"), "pe_iv": g("pe_iv"), "ce_oi": g("ce_oi"),
                      "pe_oi": g("pe_oi"), "ce_delta": g("ce_delta"), "pe_delta": g("pe_delta"),
                      "ce_ltp": g("ce_ltp"), "pe_ltp": g("pe_ltp")}).dropna(subset=["strike"]).sort_values("strike")
    for c in ("ce_iv", "pe_iv"):
        x.loc[(x[c] <= 0.5) | (x[c] > 200), c] = np.nan            # broker sends 0 for dead strikes
    x["iv"] = np.where(x.strike < spot, x.pe_iv, x.ce_iv)           # OTM side
    x["iv"] = x["iv"].fillna(x[["ce_iv", "pe_iv"]].mean(axis=1))
    x["ratio"] = x.pe_iv / x.ce_iv
    x.attrs["spot"] = spot
    return x


def _iv_at_delta(x: pd.DataFrame, side: str, target: float) -> float:
    d = x[f"{side}_delta"].abs()
    ok = d.notna() & x[f"{side}_iv"].notna() & (d > 0.03)
    if ok.sum() < 2:
        return np.nan
    y = x[ok]
    return float(y.iloc[(y[f"{side}_delta"].abs() - target).abs().argsort()[:2]][f"{side}_iv"].mean())


def metrics(x: pd.DataFrame, step: float) -> dict:
    spot = x.attrs["spot"]
    atm = x.iloc[(x.strike - spot).abs().argsort()[:2]]
    atm_iv = float(np.nanmean([atm.ce_iv.mean(), atm.pe_iv.mean()]))
    p25, c25 = _iv_at_delta(x, "pe", 0.25), _iv_at_delta(x, "ce", 0.25)
    rr = p25 - c25 if np.isfinite(p25) and np.isfinite(c25) else np.nan
    w = lambda k: float(np.interp(k, x.strike, x.iv.interpolate(limit_direction="both"))) if x.iv.notna().sum() > 2 else np.nan
    put_wing = (w(spot - 300) - atm_iv) / 3.0 if np.isfinite(atm_iv) else np.nan      # IV pts per 100 pts OTM
    call_wing = (w(spot + 300) - atm_iv) / 3.0 if np.isfinite(atm_iv) else np.nan
    near = x[(x.strike - spot).abs() <= 12 * step]
    lo = near.loc[near.iv.idxmin()] if near.iv.notna().any() else None
    return dict(atm_iv=atm_iv, rr=rr, p25=p25, c25=c25, put_wing=put_wing, call_wing=call_wing,
                low_k=float(lo.strike) if lo is not None else np.nan, low_iv=float(lo.iv) if lo is not None else np.nan, spot=spot)


def chart(x: pd.DataFrame, prev: list, opt: dict, m: dict, step: float) -> go.Figure:
    spot = x.attrs["spot"]
    xs = (x.strike - round(spot / step) * step) / step if opt["atm_axis"] else x.strike
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    if opt["oi"]:
        fig.add_trace(go.Bar(x=xs, y=x.pe_oi, name="Put OI", marker_color="rgba(239,68,68,0.35)", hovertemplate="Put OI %{y:,.0f}<extra></extra>"),
                      secondary_y=True)
        fig.add_trace(go.Bar(x=xs, y=x.ce_oi, name="Call OI", marker_color="rgba(34,197,94,0.35)", hovertemplate="Call OI %{y:,.0f}<extra></extra>"),
                      secondary_y=True)
    for (lab, px_), col in zip(prev, PREV_COLS):
        xp = (px_.strike - round(px_.attrs["spot"] / step) * step) / step if opt["atm_axis"] else px_.strike
        fig.add_trace(go.Scatter(x=xp, y=px_.iv, name=lab, line=dict(color=col, width=1.2, dash="dot"),
                                 hovertemplate=lab + " IV %{y:.1f}<extra></extra>"), secondary_y=False)
    if opt["sides"]:
        fig.add_trace(go.Scatter(x=xs, y=x.ce_iv, name="Call IV", line=dict(color=GREEN, width=1), hovertemplate="Call IV %{y:.1f}<extra></extra>"))
        fig.add_trace(go.Scatter(x=xs, y=x.pe_iv, name="Put IV", line=dict(color=RED, width=1), hovertemplate="Put IV %{y:.1f}<extra></extra>"))
    fig.add_trace(go.Scatter(x=xs, y=x.iv, name="IV (OTM side)", line=dict(color=BLUE, width=3, shape="spline"),
                             customdata=x.strike, hovertemplate="%{customdata:,.0f} · IV %{y:.2f}<extra></extra>"), secondary_y=False)
    if opt["ratio"]:
        fig.add_trace(go.Scatter(x=xs, y=x.ratio * 10, name="Vol ratio ×10 (put IV ÷ call IV)", line=dict(color=ORANGE, width=1.2, dash="dash"),
                                 customdata=x.ratio, hovertemplate="put ÷ call IV %{customdata:.2f}<extra></extra>"), secondary_y=False)
    sx = 0 if opt["atm_axis"] else spot
    fig.add_vline(x=sx, line=dict(color=WHITE, dash="dot", width=1))
    fig.add_annotation(x=sx, y=1.04, yref="paper", text=f"<b>Spot: {spot:,.2f}</b>", showarrow=False, bgcolor="#334155",
                       font=dict(color=WHITE, size=11))
    if opt["lowest"] and np.isfinite(m["low_k"]):
        lx = (m["low_k"] - round(spot / step) * step) / step if opt["atm_axis"] else m["low_k"]
        fig.add_trace(go.Scatter(x=[lx], y=[m["low_iv"]], mode="markers+text", name="Lowest IV", text=[f"low {m['low_k']:,.0f}"],
                                 textposition="bottom center", marker=dict(color=GOLD, size=12, symbol="diamond"),
                                 textfont=dict(color=GOLD)), secondary_y=False)
    ol._layout(fig, "Volatility Skew", h=500)
    fig.update_layout(barmode="overlay", hovermode="x unified", margin=dict(l=10, r=10, t=55, b=10))
    fig.update_yaxes(title_text="IV %", tickformat=".0f", secondary_y=False)
    fig.update_yaxes(title_text="OI", tickformat="~s", showgrid=False, secondary_y=True)
    fig.update_xaxes(title_text="strikes from ATM" if opt["atm_axis"] else "strike", tickformat=",.0f" if not opt["atm_axis"] else "+d")
    return fig


def narrative(m: dict, m_open: dict | None, m_prev: dict | None, move: float) -> tuple[str, str, list, list]:
    L, do = [], []
    d = lambda a, b, k: (a[k] - b[k]) if (b and np.isfinite(a.get(k, np.nan)) and np.isfinite(b.get(k, np.nan))) else np.nan
    div_o, div_p = d(m, m_open, "atm_iv"), d(m, m_prev, "atm_iv")
    drr_o, drr_p = d(m, m_open, "rr"), d(m, m_prev, "rr")
    L.append(f"ATM IV <b>{m['atm_iv']:.2f}</b>" + (f" · since open {div_o:+.2f}" if np.isfinite(div_o) else "")
             + (f" · vs previous session {div_p:+.2f}" if np.isfinite(div_p) else "") + ".")
    if np.isfinite(m["rr"]):
        L.append(f"25-delta risk reversal (put IV − call IV) <b>{m['rr']:+.2f}</b>" + (f" · since open {drr_o:+.2f}" if np.isfinite(drr_o) else "")
                 + (f" · vs previous session {drr_p:+.2f}" if np.isfinite(drr_p) else "")
                 + " — NIFTY normally carries a positive put skew (protection costs more than upside).")
    if np.isfinite(m["put_wing"]) and np.isfinite(m["call_wing"]):
        L.append(f"Wings: put side {m['put_wing']:+.2f} IV pts per 100 pts OTM · call side {m['call_wing']:+.2f}.")
    if np.isfinite(m["low_k"]):
        L.append(f"Lowest IV at <b>{m['low_k']:,.0f}</b> ({m['low_k'] - m['spot']:+,.0f} vs spot) — the strike the market "
                 f"treats as 'home'; a bottom ABOVE spot after a fall = upside is being priced as the calmer path.")
    # intraday change leads; the previous-session change is only a fallback (half weight)
    r_ch = drr_o if np.isfinite(drr_o) else (0.5 * drr_p if np.isfinite(drr_p) else np.nan)
    i_ch = div_o if np.isfinite(div_o) else (0.5 * div_p if np.isfinite(div_p) else np.nan)
    rr_up, rr_dn = bool(np.isfinite(r_ch) and r_ch > 0.4), bool(np.isfinite(r_ch) and r_ch < -0.4)
    iv_up, iv_dn = bool(np.isfinite(i_ch) and i_ch > 0.4), bool(np.isfinite(i_ch) and i_ch < -0.4)
    if rr_up:
        v, c = ("FEAR BUILDING — put skew steepening + ATM IV rising" if iv_up else
                "PUT SKEW STEEPENING — hedging demand rising (ATM IV steady)"), RED
        do += ["Down-moves are being paid for: PE on breaks of support has the wind (put IV helps the put you hold).",
               "Avoid bottom-fishing CE while puts keep getting dearer — that bounce usually gets sold."]
    elif rr_dn:
        v, c = "FEAR FADING — put skew flattening" + (" / IV easing" if iv_dn else ""), GREEN if move < 0 else GOLD
        do += ["After a fall this is capitulation ending: fresh PE at the lows now fights falling put IV (vega loss) — avoid.",
               "A CE on a confirmed reversal candle has IV on its side; book PE profits."]
    elif np.isfinite(m["rr"]) and m["rr"] < 0:
        v, c = "CALLS BID — call IV above put IV (upside chase)", GREEN
        do += ["Rare for NIFTY: short-covering / upside squeeze being priced. CE momentum trades work; don't short into it."]
    elif iv_dn and not rr_up:
        v, c = "CALM — IV falling, skew stable (sellers' tape)", GOLD
        do += ["Buyers bleed on calm days — only buy at a wall break with a quick target; ITM strikes."]
    else:
        v, c = "NORMAL — skew and IV steady", GOLD
        do += ["No volatility edge either way: let price action and the writers' flow decide."]
    do.append("Skew tells you how the market is PAYING for risk, not the direction by itself — confirm with your setup.")
    return v, c, L, do


def _card(title: str, body: str, border: str = GOLD) -> str:
    return (f"<div style='background:{NAVY};border:1px solid {border};border-radius:8px;padding:10px 14px;margin:6px 0'>"
            f"<div style='color:{GOLD};font-weight:700'>{title}</div>"
            f"<div style='color:{WHITE};margin-top:4px;line-height:1.6'>{body}</div></div>")


HOW = ["<b>The smile</b>: IV of the out-of-the-money option at each strike (puts below spot, calls above). Higher = that "
       "strike's protection costs more.",
       "<b>Put skew</b> (left side higher) is normal for NIFTY. It STEEPENING with ATM IV rising = fear building, down-moves "
       "being paid for. It FLATTENING after a fall = panic leaving — puts lose value even if price stalls.",
       "<b>25-delta risk reversal</b> = IV of the ≈0.25-delta put − IV of the ≈0.25-delta call. Rising = puts dearer; "
       "negative = calls dearer (upside squeeze).",
       "<b>Lowest-IV strike</b> = the smile's bottom; where it sits vs spot shows which way the market thinks is calmer.",
       "<b>Previous days</b>: the whole curve shifting UP = more fear than yesterday; DOWN = calmer.",
       "<b>Replay</b> the slider to see how the smile changed before a big move — skew often moves first.",
       "<b>Vol ratio</b> = put IV ÷ call IV at the same strike (×10 on the chart). Above 1 = puts richer there.",
       "For an option BUYER: buy when IV is low and starting to rise in your direction; avoid buying the side whose IV "
       "is collapsing (you pay for fear that is leaving)."]


def render(day: date | None = None) -> None:
    import streamlit as st
    import store
    import config

    days = ol._days_available()
    if not days:
        st.info("No option-chain snapshots stored yet.")
        return

    def _n(d):
        return len(list((Path(config.SNAPSHOT_DIR) / d.strftime("%Y-%m-%d")).glob("*.meta.json")))

    @st.cache_data(ttl=30, show_spinner=False)
    def _load(d: date, n: int):
        return store.load_day(d)

    step = float(ol._rules().get("strike_step", 50) or 50)
    c = st.columns([1.5, 1.3, 1.3, 1.2, 1.2, 1.2, 1.2])
    day = c[0].selectbox("Session", days, index=days.index(day) if day in days else 0,
                         format_func=lambda d: d.strftime("%a %d %b %Y"), key="vsk_day")
    axis = c[1].radio("X-Axis View", ["Strike", "ATM±"], horizontal=True, key="vsk_axis")
    nprev = c[2].selectbox("Previous Days (T-Days)", [0, 1, 2, 3, 5], index=1, key="vsk_prev")
    lowest = c[3].toggle("Show Lowest IV", value=True, key="vsk_low")
    ratio = c[4].toggle("Vol Ratio", value=False, key="vsk_ratio")
    sides = c[5].toggle("Call / Put IV", value=False, key="vsk_sides")
    show_oi = c[6].toggle("Call / Put OI", value=True, key="vsk_oi")
    snaps = _load(day, _n(day))
    if len(snaps) < 1:
        st.warning("No snapshots for this session.")
        return
    times = [pd.Timestamp(s[0]) for s in snaps]
    t = st.select_slider("TIME (replay)", options=times, value=times[-1], format_func=lambda z: z.strftime("%H:%M"), key="vsk_t")
    i = times.index(t)
    ts, spot, df = snaps[i]
    exp = str(ol._one_expiry(df)["expiry"].iloc[0]) if "expiry" in df else None
    x = frame(df, float(spot), exp)
    if x.iv.notna().sum() < 5:
        st.warning("The chain at this time has no usable IV.")
        return
    m = metrics(x, step)
    m_open = metrics(frame(snaps[0][2], float(snaps[0][1]), exp), step)
    prev, m_prev = [], None
    older = [d for d in days if d < day][: max(nprev, 1)]
    for j, d in enumerate(older):
        sp = _load(d, _n(d))
        if not sp:
            continue
        ts_p, spot_p, df_p = sp[-1]
        e_p = exp if ("expiry" in df_p and (df_p["expiry"].astype(str) == str(exp)).any()) else None
        xp = frame(df_p, float(spot_p), e_p)
        if j == 0:
            m_prev = metrics(xp, step)
        if j < nprev:
            prev.append((f"T-{j + 1} {d:%d %b}" + ("" if e_p else " (other expiry)"), xp))
    move = float(spot) - float(snaps[0][1])
    v, vc, L, do = narrative(m, m_open, m_prev, move)
    k = st.columns(5)
    k[0].metric("ATM IV", f"{m['atm_iv']:.2f}", f"{m['atm_iv'] - m_open['atm_iv']:+.2f} since open" if np.isfinite(m_open["atm_iv"]) else None)
    k[1].metric("25Δ risk reversal", f"{m['rr']:+.2f}" if np.isfinite(m["rr"]) else "—",
                f"{m['rr'] - m_open['rr']:+.2f} since open" if np.isfinite(m["rr"]) and np.isfinite(m_open["rr"]) else None,
                delta_color="inverse")
    k[2].metric("Put wing / 100 pts", f"{m['put_wing']:+.2f}" if np.isfinite(m["put_wing"]) else "—")
    k[3].metric("Call wing / 100 pts", f"{m['call_wing']:+.2f}" if np.isfinite(m["call_wing"]) else "—")
    k[4].metric("Lowest IV strike", f"{m['low_k']:,.0f}" if np.isfinite(m["low_k"]) else "—",
                f"{m['low_k'] - m['spot']:+,.0f} vs spot" if np.isfinite(m["low_k"]) else None, delta_color="off")
    st.plotly_chart(chart(x, prev, dict(atm_axis=axis != "Strike", lowest=lowest, ratio=ratio, sides=sides, oi=show_oi), m, step),
                    use_container_width=True, key="vsk_chart")
    st.markdown(_card(f"★ PRO READ — <span style='color:{vc}'>{v}</span> · {pd.Timestamp(ts):%H:%M}", "<br>".join(L), border=vc),
                unsafe_allow_html=True)
    st.markdown(_card("WHAT TO DO WITH IT", "<ul style='margin:0 0 0 18px'>" + "".join(f"<li>{d}</li>" for d in do) + "</ul>"),
                unsafe_allow_html=True)
    with st.expander("How to read the volatility skew"):
        st.markdown("<ul>" + "".join(f"<li style='margin:3px 0'>{h}</li>" for h in HOW) + "</ul>", unsafe_allow_html=True)
    st.caption(f"Expiry {exp} · information only — not financial advice. Not backtested in this app.")
