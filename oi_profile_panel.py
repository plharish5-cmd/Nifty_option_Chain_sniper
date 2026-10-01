"""
oi_profile_panel.py — StockMojo-style "Open Interest" view, from YOUR stored
snapshots. First sub-tab of 🧪 Options Lab.

  * Strike-wise bars: Call OI (green) and Put OI (red) for each strike.
      solid part   = OI that was already there at the START of the window
      striped top  = OI ADDED during the window   (fresh writing)
      hollow top   = OI REMOVED during the window (writers exiting)
    Black dotted line = spot, orange line = max pain.
  * Time window: Last 3 / 5 / 10 / 15 / 30 min, 1 / 2 / 3 hr, or All (vs yesterday's close),
    or a custom From–To range.
  * Market Sentiment gauge (OI based) + Market Insight + Analysis written from the numbers.
  * Cards: OI Change (Call vs Put), Total OI (Call vs Put), PCR donut.
  * Every chart has a "How to read" box.

Reads stored snapshots only. Weights in strategy1_rules.json → "oi_profile".
Information only — not financial advice.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go

import config
import options_lab_tab as ol
from options_lab_tab import fmt, NAVY, PANEL, GRID, GOLD, WHITE, MUTED, GREEN, RED

RULES_PATH = Path(__file__).parent / "strategy1_rules.json"
_DEF = {"default_strikes": 10, "strike_step": 50,
        "w_pcr_level": 0.3, "w_pcr_change": 0.3, "w_oi_balance": 0.4,
        "pcr_level_scale": 0.4, "pcr_change_scale": 0.2,
        "very_threshold": 0.6, "lean_threshold": 0.2}
WINDOWS = {"Last 3 min": 3, "Last 5 min": 5, "Last 10 min": 10, "Last 15 min": 15, "Last 30 min": 30,
           "Last 1 hr": 60, "Last 2 hr": 120, "Last 3 hr": 180, "All": None}


def _rules() -> dict:
    try:
        r = json.loads(RULES_PATH.read_text()).get("oi_profile", {})
    except Exception:
        r = {}
    out = {**_DEF, **{k: v for k, v in r.items() if not k.startswith("_")}}
    out["oi_multiplier"] = float(ol._rules()["oi_multiplier"])
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 1 · Compute
# ═══════════════════════════════════════════════════════════════════════════
def _chain(df: pd.DataFrame, mult: float) -> pd.DataFrame:
    d = ol._one_expiry(df)[["strike"] + [c for c in ("ce_oi", "pe_oi", "ce_chg_oi", "pe_chg_oi") if c in df]].copy()
    for c in ("ce_oi", "pe_oi", "ce_chg_oi", "pe_chg_oi"):
        if c in d:
            d[c] = pd.to_numeric(d[c], errors="coerce").fillna(0) * mult
    return d.set_index("strike")


def profile(snaps: list, i_end: int, window_min: int | None, i_start: int | None,
            lo: float | None, hi: float | None, mult: float) -> dict:
    """OI at window start vs end, per strike."""
    t1, spot1, df1 = snaps[i_end]
    end = _chain(df1, mult)
    if window_min is None and i_start is None:                 # All → vs yesterday's close
        start = end.copy()
        if "ce_chg_oi" in end:
            start["ce_oi"] = end["ce_oi"] - end["ce_chg_oi"]
            start["pe_oi"] = end["pe_oi"] - end["pe_chg_oi"]
        else:
            start = _chain(snaps[0][2], mult)
        t0, spot0, label = None, None, "yesterday's close"
        pcr0_full = None
    else:
        if i_start is None:
            target = t1 - timedelta(minutes=window_min)
            i_start = max([i for i, s in enumerate(snaps[: i_end + 1]) if s[0] <= target], default=0)
        t0, spot0, df0 = snaps[i_start]
        start = _chain(df0, mult)
        label = f"{t0:%H:%M}"
    p = pd.DataFrame({"ce1": end["ce_oi"], "pe1": end["pe_oi"]}).join(
        pd.DataFrame({"ce0": start["ce_oi"], "pe0": start["pe_oi"]}), how="left").fillna(0)
    p = p.reset_index().rename(columns={"index": "strike"}).sort_values("strike")
    full = p.copy()
    if lo is not None and hi is not None:
        p = p[(p["strike"] >= lo) & (p["strike"] <= hi)]
    p["dce"], p["dpe"] = p["ce1"] - p["ce0"], p["pe1"] - p["pe0"]
    # max pain on the FULL chain at window end
    ks = full["strike"].to_numpy(float)
    pain = [(np.maximum(k - ks, 0) * full["ce1"]).sum() + (np.maximum(ks - k, 0) * full["pe1"]).sum() for k in ks]
    mp = float(ks[int(np.argmin(pain))]) if len(ks) else None
    ce1, pe1, ce0, pe0 = p["ce1"].sum(), p["pe1"].sum(), p["ce0"].sum(), p["pe0"].sum()
    return dict(p=p, t1=t1, t0=t0, spot=float(spot1), spot0=spot0, start_label=label, max_pain=mp,
                ce1=ce1, pe1=pe1, dce=ce1 - ce0, dpe=pe1 - pe0,
                pcr1=pe1 / ce1 if ce1 else np.nan, pcr0=pe0 / ce0 if ce0 else np.nan)


def sentiment(o: dict, r: dict) -> tuple[float, str]:
    """Score −1 (very bearish) … +1 (very bullish), from OI only."""
    lvl = np.clip((o["pcr1"] - 1.0) / r["pcr_level_scale"], -1, 1)
    chg = np.clip((o["pcr1"] - o["pcr0"]) / r["pcr_change_scale"], -1, 1) if pd.notna(o["pcr0"]) else 0
    tot = abs(o["dpe"]) + abs(o["dce"])
    bal = (o["dpe"] - o["dce"]) / tot if tot else 0
    s = float(r["w_pcr_level"] * lvl + r["w_pcr_change"] * chg + r["w_oi_balance"] * bal)
    v, l = r["very_threshold"], r["lean_threshold"]
    label = ("Very Bullish" if s >= v else "Bullish" if s >= l else
             "Very Bearish" if s <= -v else "Bearish" if s <= -l else "Neutral")
    return s, label


def analysis(o: dict, label: str) -> tuple[str, str]:
    p = o["p"]
    dce, dpe = o["dce"], o["dpe"]
    px = (o["spot"] - o["spot0"]) if o["spot0"] else None
    mv = f" while NIFTY moved {px:+.0f} pts" if px is not None else ""
    if dce > 0 and dpe > 0:
        what = f"Fresh OI on both sides (calls {fmt(dce):s}, puts {fmt(dpe)})"
        what += (" — call writing is heavier, capping the upside" if dce > dpe else
                 " — put writing is heavier, building a floor")
    elif dce < 0 and dpe < 0:
        what = f"OI leaving both sides (calls {fmt(dce)}, puts {fmt(dpe)}) — positions closing, expect a quieter market"
    elif dce < 0 <= dpe:
        what = f"Call writers exiting ({fmt(dce)}) while put writers add ({fmt(dpe)}) — short covering + support building, bullish"
    else:
        what = f"Put writers exiting ({fmt(dpe)}) while call writers add ({fmt(dce)}) — support weakening + ceiling building, bearish"
    pcr_txt = (f" PCR moved from {o['pcr0']:.2f} to {o['pcr1']:.2f}." if pd.notna(o["pcr0"]) else f" PCR {o['pcr1']:.2f}.")
    res = p.loc[p["ce1"].idxmax()]
    sup = p.loc[p["pe1"].idxmax()]
    add_c = p.loc[p["dce"].idxmax()]
    add_p = p.loc[p["dpe"].idxmax()]
    walls = (f" Biggest call wall (resistance) **{res.strike:,.0f}** ({fmt(res.ce1)}); biggest put wall (support) "
             f"**{sup.strike:,.0f}** ({fmt(sup.pe1)}). Most calls added at **{add_c.strike:,.0f}** ({fmt(add_c.dce)}), "
             f"most puts added at **{add_p.strike:,.0f}** ({fmt(add_p.dpe)}).")
    mp = o["max_pain"]
    mp_txt = (f" Max pain **{mp:,.0f}** is {abs(o['spot'] - mp):.0f} pts {'below' if mp < o['spot'] else 'above'} spot "
              f"— on expiry day price often drifts toward it.") if mp else ""
    insight = {
        "Very Bullish": "Market showing strong bullish OI build-up — put writers firmly in control.",
        "Bullish": "Market leaning bullish — put side stronger than call side.",
        "Neutral": "Balanced OI — no side in control; expect range trading between the walls.",
        "Bearish": "Market leaning bearish — call side stronger than put side.",
        "Very Bearish": "Market showing strong bearish sentiment — call writers firmly in control.",
    }[label]
    return insight, what + mv + "." + pcr_txt + walls + mp_txt


# ═══════════════════════════════════════════════════════════════════════════
# 2 · Charts
# ═══════════════════════════════════════════════════════════════════════════
def chart_profile(o: dict, step: float):
    p = o["p"]
    w, off = step * 0.4, step * 0.21
    fig = go.Figure()

    def side(x, oi0, oi1, color, name):
        solid = np.minimum(oi0, oi1)
        inc = np.maximum(oi1 - oi0, 0)
        dec = np.maximum(oi0 - oi1, 0)
        cd = np.c_[[fmt(v) for v in oi1], [fmt(v) for v in oi1 - oi0], p["strike"]]
        ht = f"<b>%{{customdata[2]:,.0f}} {name}</b><br>OI %{{customdata[0]}}<br>Change %{{customdata[1]}}<extra></extra>"
        fig.add_trace(go.Bar(x=x, y=solid, width=w, marker_color=color, name=f"{name} OI",
                             customdata=cd, hovertemplate=ht))
        fig.add_trace(go.Bar(x=x, y=inc, base=solid, width=w, name=f"{name} OI increase",
                             marker=dict(color="rgba(255,255,255,0.9)", line=dict(color=color, width=1),
                                         pattern=dict(shape="/", fgcolor=color, size=6, solidity=0.45)),
                             customdata=cd, hovertemplate=ht))
        fig.add_trace(go.Bar(x=x, y=dec, base=solid, width=w, name=f"{name} OI decrease",
                             marker=dict(color="rgba(255,255,255,0.95)", line=dict(color=color, width=1.6)),
                             customdata=cd, hovertemplate=ht))

    side(p["strike"] - off, p["ce0"], p["ce1"], GREEN, "Call")
    side(p["strike"] + off, p["pe0"], p["pe1"], RED, "Put")
    fig.add_vline(x=o["spot"], line=dict(color=WHITE, dash="dot", width=1.6),
                  annotation_text=f"Spot {o['spot']:,.2f}", annotation_position="top",
                  annotation_font_color=WHITE, annotation_bgcolor=NAVY)
    if o["max_pain"]:
        fig.add_vline(x=o["max_pain"], line=dict(color="#f59e0b", dash="dash", width=1.6),
                      annotation_text=f"Max Pain {o['max_pain']:,.0f}", annotation_position="top right",
                      annotation_font_color="#000", annotation_bgcolor="#f59e0b")
    ymax = float(np.maximum(p[["ce0", "ce1"]].max(axis=1), p[["pe0", "pe1"]].max(axis=1)).max())
    ticks = np.linspace(0, ymax, 6)
    fig.update_layout(barmode="overlay", hovermode="closest",
                      legend=dict(orientation="h", y=-0.22, x=0.5, xanchor="center", font=dict(color=WHITE)))
    fig.update_xaxes(tickvals=p["strike"], ticktext=[f"{k:,.0f}" for k in p["strike"]], tickangle=-45)
    fig.update_yaxes(tickvals=ticks, ticktext=[fmt(v) for v in ticks])
    title = f"Open Interest by strike · change from {o['start_label']} → {o['t1']:%H:%M}"
    return ol._layout(fig, title, h=500)


def chart_gauge(score: float, label: str):
    pct = abs(score) * 100
    col = GREEN if score > 0.2 else RED if score < -0.2 else GOLD
    fig = go.Figure(go.Pie(values=[max(pct, 1), 100 - max(pct, 1)], hole=0.72, sort=False, direction="clockwise",
                           marker=dict(colors=[col, "#1e293b"]), textinfo="none", hoverinfo="skip"))
    fig.add_annotation(text=f"<b>{label}</b><br><span style='font-size:12px;color:{MUTED}'>{pct:.0f}% strength</span>",
                       showarrow=False, font=dict(size=18, color=col))
    fig.update_layout(showlegend=False, paper_bgcolor=NAVY, height=240, margin=dict(l=10, r=10, t=10, b=10))
    return fig


def chart_pair(title: str, call: float, put: float):
    fig = go.Figure(go.Bar(x=["CALL", "PUT"], y=[call, put], marker_color=[GREEN, RED],
                           text=[fmt(call), fmt(put)], textposition="outside", textfont=dict(color=WHITE)))
    lo = min(0, call, put) * 1.25
    hi = max(0, call, put) * 1.25 or 1
    ticks = np.linspace(lo, hi, 5)
    fig.update_yaxes(range=[lo, hi], tickvals=ticks, ticktext=[fmt(v) for v in ticks])
    fig.update_layout(showlegend=False)
    return ol._layout(fig, title, h=300)


def chart_pcr_donut(ce: float, pe: float):
    tot = ce + pe or 1
    fig = go.Figure(go.Pie(labels=["Put OI", "Call OI"], values=[pe, ce], hole=0.6, sort=False,
                           marker=dict(colors=[RED, GREEN]), texttemplate="%{percent:.0%}<br>%{label}",
                           textfont=dict(color=WHITE)))
    fig.add_annotation(text=f"PCR<br><b>{pe / ce if ce else 0:.2f}</b>", showarrow=False, font=dict(size=20, color=WHITE))
    fig.update_layout(showlegend=False)
    return ol._layout(fig, "Put / Call Ratio", h=300)


# ═══════════════════════════════════════════════════════════════════════════
# 3 · How to read
# ═══════════════════════════════════════════════════════════════════════════
GUIDE = {
    "profile": ("Each strike has two bars: Call OI (green, left) and Put OI (red, right). Writers sell calls where they think price "
                "will NOT go above, and puts where they think it will NOT go below.",
                ["Tallest GREEN bar above spot = resistance (call wall). Tallest RED bar below spot = support (put wall)",
                 "STRIPED top = OI added in the chosen window → that wall is getting STRONGER",
                 "HOLLOW top = OI removed in the window → that wall is getting WEAKER (writers running)",
                 "Hollow green just above spot + striped red just below = short covering + support → bullish",
                 "Striped green just above spot + hollow red just below = fresh ceiling + support breaking → bearish",
                 "Orange line = max pain (strike where option buyers lose most); expiry-day price often drifts toward it",
                 "Use 'Last 15 / 30 min' to see what changed recently — 'All' shows the whole day vs yesterday"]),
    "sentiment": ("A single OI-based score combining PCR level, PCR change in the window, and which side added more OI.",
                  ["Very Bullish / Bullish = put side dominating", "Very Bearish / Bearish = call side dominating",
                   "% = strength of the lean (not a probability)",
                   "It uses OI only — confirm with price (candles, levels) before acting"]),
    "cards": ("Totals for the strike range shown, so you can compare call vs put at a glance.",
              ["OI Change: which side wrote more in the window — bigger GREEN = bearish, bigger RED = bullish",
               "Total OI: where the weight sits overall",
               "PCR donut: Put OI share vs Call OI share. PCR above 1 = more puts = bullish lean; below 0.7 = bearish lean"]),
}


def _box(key: str, now_html: str = "", vote: str | None = None):
    import streamlit as st
    what, rules = GUIDE[key]
    li = "".join(f"<li>{x}</li>" for x in rules)
    badge = ""
    if vote:
        c = GREEN if "Bull" in vote else RED if "Bear" in vote else GOLD
        badge = f"<span style='background:{c};color:#000;font-weight:700;padding:1px 8px;border-radius:4px'>{vote}</span>&nbsp;"
    now = (f"<div style='margin-top:8px'><span style='color:{GOLD};font-weight:700'>RIGHT NOW · </span>{badge}"
           f"{ol._md_bold(now_html)}</div>") if now_html else ""
    st.markdown(
        f"""<div style="background:{NAVY};border-left:4px solid {GOLD};border-radius:6px;padding:12px 16px;
        margin:-4px 0 20px 0;color:{WHITE};font-size:0.92rem;line-height:1.5">
        <div><span style="color:{GOLD};font-weight:700">WHAT IT IS · </span>{what}</div>{now}
        <div style="margin-top:8px;color:{GOLD};font-weight:700">HOW TO READ</div>
        <ul style="margin:4px 0 0 18px;color:#cbd5e1">{li}</ul></div>""", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════════
# 4 · Render
# ═══════════════════════════════════════════════════════════════════════════
def render(day: date | None = None) -> None:
    import streamlit as st
    import store

    r = _rules()
    days = ol._days_available()
    if not days:
        st.info("No option-chain snapshots stored yet.")
        return
    day = day or days[0]

    @st.cache_data(ttl=60, show_spinner=False)
    def _snaps(d: date, n_files: int):
        return store.load_day(d)

    n_files = len(list((Path(config.SNAPSHOT_DIR) / day.strftime("%Y-%m-%d")).glob("*.meta.json")))
    snaps = _snaps(day, n_files)
    if len(snaps) < 2:
        st.warning("Not enough snapshots for this session yet.")
        return
    times = [s[0] for s in snaps]

    c = st.columns([2.2, 4.8])
    opts = ["All", "ATM", 5, 10, 15, 20]
    n = c[0].radio("Strikes above / below ATM", opts, index=opts.index(r["default_strikes"]) if r["default_strikes"] in opts else 3,
                   horizontal=True, key="oip_n")
    win = c[1].radio("Window", list(WINDOWS), index=len(WINDOWS) - 1, horizontal=True, key="oip_win")
    custom = st.toggle("Custom From – To time range", value=False, key="oip_custom")
    i_start = None
    i_end = len(snaps) - 1
    if custom:
        a, b = st.select_slider("Time range", options=list(range(len(times))), value=(0, len(times) - 1),
                                format_func=lambda i: f"{times[i]:%H:%M}", key="oip_range")
        i_start, i_end = a, max(b, a + 1) if a + 1 < len(times) else b

    spot = snaps[i_end][1]
    step = float(r["strike_step"])
    atm = round(spot / step) * step
    if n == "All":
        lo = hi = None
    elif n == "ATM":
        lo, hi = atm - 2 * step, atm + 2 * step
    else:
        lo, hi = atm - int(n) * step, atm + int(n) * step

    o = profile(snaps, i_end, None if custom else WINDOWS[win], i_start, lo, hi, r["oi_multiplier"])
    if o["p"].empty:
        st.warning("No strikes in that range.")
        return
    score, label = sentiment(o, r)
    insight, text = analysis(o, label)

    st.plotly_chart(chart_profile(o, step), width="stretch", key="oip_profile")
    _box("profile")

    left, right = st.columns([1, 2])
    with left:
        st.plotly_chart(chart_gauge(score, label), width="stretch", key="oip_gauge")
        pcr_d = o["pcr1"] - o["pcr0"] if pd.notna(o["pcr0"]) else np.nan
        dc = RED if (pd.notna(pcr_d) and pcr_d < 0) else GREEN
        st.markdown(
            f"""<div style="background:{PANEL};border:1px solid {GRID};border-radius:6px;padding:8px;text-align:center;color:{WHITE}">
            PCR: <b>{o['pcr1']:.2f}</b>{f"<span style='color:{dc}'> ({pcr_d:+.2f})</span>" if pd.notna(pcr_d) else ""}<br>
            PCR of OI change (Put chg ÷ Call chg): <b>{(o['dpe'] / o['dce']) if o['dce'] else float('nan'):.2f}</b></div>""",
            unsafe_allow_html=True)
    with right:
        st.markdown(
            f"""<div style="background:{NAVY};border:1px solid {GOLD};border-radius:8px;padding:12px 16px;color:{WHITE};margin-bottom:10px">
            <div style="color:{GOLD};font-weight:700">ⓘ MARKET INSIGHT</div><div style="margin-top:4px">{insight}</div></div>
            <div style="background:{NAVY};border:1px solid {GRID};border-radius:8px;padding:12px 16px;color:{WHITE}">
            <div style="color:{GOLD};font-weight:700">ⓘ ANALYSIS</div><div style="margin-top:4px">{ol._md_bold(text)}</div></div>""",
            unsafe_allow_html=True)
    _box("sentiment", f"Score {score:+.2f} → {label}.", label)

    k1, k2, k3 = st.columns(3)
    k1.plotly_chart(chart_pair("Open Interest Change", o["dce"], o["dpe"]), width="stretch", key="oip_chg")
    k2.plotly_chart(chart_pair("Total Open Interest", o["ce1"], o["pe1"]), width="stretch", key="oip_tot")
    k3.plotly_chart(chart_pcr_donut(o["ce1"], o["pe1"]), width="stretch", key="oip_pcr")
    side = "CALL side added more → bearish lean" if o["dce"] > o["dpe"] else "PUT side added more → bullish lean"
    _box("cards", f"Change since {o['start_label']}: Call **{fmt(o['dce'])}**, Put **{fmt(o['dpe'])}** → {side}. "
                  f"Total: Call **{fmt(o['ce1'])}**, Put **{fmt(o['pe1'])}**.")
    st.caption("Information only — not financial advice.")
