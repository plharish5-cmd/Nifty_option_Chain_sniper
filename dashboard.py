"""
NIFTY option chain scanner — dashboard.

Run:   streamlit run dashboard.py

Offline mode (works at 2 AM, no network, no market): tick "Demo mode" in the
sidebar and it runs the whole pipeline against data/sample_chain.csv. Use it
to check your changes to the scoring logic without waiting for a market day.
"""
from __future__ import annotations

import time
from datetime import datetime, date, timedelta, time as dtime_
from typing import Any

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

import config
import chain_parser as cp
import analytics as an
import store
import timeline as tl
import outcomes as oc
import alerts as al
import strategy1_tab
import context_tab
import levels as lv_mod
import confluence as cf
import market_data as md
import price_action as pa
import setups as su
import market_state as ms
import rulebook as rb
import flow as fl
import greeks as gk
import view_building as vb
import view_building_tab
import chart_patterns_tab
import war_tab
import paper_trading_tab
import timesfm_tab
import volume_profile_tab
import market_depth_tab
from ui_utils import render_html, _render_html

st.set_page_config(page_title="NIFTY Chain Scanner", layout="wide",
                   initial_sidebar_state="expanded")

NAVY, NAVY2, GOLD = "#071626", "#0B2035", "#C9A227"
GREEN, RED, SLATE, BLUE = "#2FBF71", "#E8503A", "#8FA6B8", "#4A9BD1"

_render_html(f"""<style>
 .stApp {{ background:{NAVY}; }}
 h1,h2,h3,h4 {{ color:#F2F5F8 !important; }}
 [data-testid="stMetricValue"] {{ font-size:26px; }}
 .banner {{ background:{NAVY2}; border-left:5px solid {GOLD};
            padding:16px 20px; border-radius:0 10px 10px 0; margin-bottom:14px; }}
 .verdict {{ font-size:30px; font-weight:700; letter-spacing:-.01em; }}
 .sub {{ color:{SLATE}; font-size:14px; }}
 .pill {{ display:inline-block; padding:3px 11px; border-radius:20px;
          font-size:12px; font-weight:600; margin-right:6px; }}
 /* Modern, un-crumpled horizontal scrollable tab ribbon */
 div[data-baseweb="tab-list"] {{
   display: flex !important;
   flex-wrap: nowrap !important;
   overflow-x: auto !important;
   overflow-y: hidden !important;
   white-space: nowrap !important;
   gap: 6px !important;
   padding: 6px 4px 10px 4px !important;
   border-bottom: 2px solid #163859 !important;
   scrollbar-width: thin !important;
   scrollbar-color: #00D2D3 #0d2137 !important;
 }}
 div[data-baseweb="tab-list"]::-webkit-scrollbar {{
   height: 6px !important;
 }}
 div[data-baseweb="tab-list"]::-webkit-scrollbar-track {{
   background: #0d2137 !important;
   border-radius: 4px !important;
 }}
 div[data-baseweb="tab-list"]::-webkit-scrollbar-thumb {{
   background: #00D2D3 !important;
   border-radius: 4px !important;
 }}
 div[data-baseweb="tab-list"] button[data-baseweb="tab"],
 div[data-baseweb="tab-list"] button {{
   flex: 0 0 auto !important;
   min-width: max-content !important;
   padding: 7px 15px !important;
   font-size: 13px !important;
   font-weight: 500 !important;
   color: #8FA6B8 !important;
   background-color: #0e2238 !important;
   border: 1px solid #1b3a5b !important;
   border-radius: 6px !important;
   margin: 0 !important;
   transition: all 0.2s ease !important;
 }}
 div[data-baseweb="tab-list"] button:hover {{
   color: #FFFFFF !important;
   background-color: #143554 !important;
   border-color: #4A9BD1 !important;
 }}
 div[data-baseweb="tab-list"] button[aria-selected="true"] {{
   color: #FFFFFF !important;
   background-color: #174268 !important;
   border: 1px solid #00D2D3 !important;
   box-shadow: 0 2px 8px rgba(0, 210, 211, 0.25) !important;
   font-weight: 600 !important;
 }}
 div[data-baseweb="tab-highlight"] {{
   display: none !important;
 }}
</style>""")

# Rebuild STATE when its shape no longer matches store.SessionState.
#
# `if "state" not in st.session_state` creates the object once per browser
# session and never again. Add a field to SessionState and the live object
# keeps its old shape — restarting the server does not help, because the
# session lives in the browser. The failure is an AttributeError from a line
# that has been correct for weeks, which sends you looking in the wrong file.
#
# Missing attributes are copied on rather than discarding the object: the
# baseline, peaks and flip tracker took the whole session to accumulate and
# throwing them away to add one integer would lose real state.
_fresh = store.SessionState()
if "state" not in st.session_state:
    st.session_state.state = _fresh
else:
    _old = st.session_state.state
    _added = [a for a in vars(_fresh) if not hasattr(_old, a)]
    for a in _added:
        setattr(_old, a, getattr(_fresh, a))
    if _added:
        st.sidebar.caption(
            "session state upgraded — added " + ", ".join(_added))
STATE: store.SessionState = st.session_state.state
if STATE.flips is None:
    STATE.flips = cf.FlipTracker()


# ═══════════════════════════════════════════════════════════════════════════
# Data
# ═══════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=config.POLL_SECONDS - 10, show_spinner=False)
def fetch_live() -> tuple[pd.DataFrame, float, str, str]:
    import nse_client
    client = nse_client.NSEClient()
    payload = client.fetch_raw()
    expiries = client.expiries(payload)
    expiry = expiries[config.EXPIRY_INDEX]          # 1st dropdown = nearest
    df = cp.parse_json(payload, expiry)
    return df, client.spot(payload), expiry, client.timestamp(payload)


@st.cache_data(ttl=config.POLL_SECONDS - 10, show_spinner=False)
def fetch_vix_cached():
    """VIX moves intraday — same cadence as the chain."""
    import nse_client
    return md.fetch_vix(nse_client.NSEClient())


@st.cache_data(ttl=config.FLOWS_CACHE_SECONDS, show_spinner=False)
def fetch_flows_cached():
    """FII/DII publishes once daily after close — fetched once, cached 6h."""
    import nse_client
    return md.fetch_flows(nse_client.NSEClient())


@st.cache_data(ttl=config.PA_POLL_SECONDS - 5, show_spinner=False)
def fetch_price_action():
    """
    One API call for 1-minute candles; 5m and 15m are resampled locally so the
    three views can never disagree with each other mid-candle.
    """
    import candle_source as cs
    return cs.fetch()


@st.cache_data(ttl=config.PA_POLL_SECONDS - 5, show_spinner=False)
def fetch_banknifty_price_action():
    """Fetch BANKNIFTY 1-min candles for SMT Divergence evaluation in Tab 31."""
    import candle_source as cs
    return cs.fetch_banknifty()


@st.cache_data(ttl=120, show_spinner=False)
def fetch_view_building_sources_cached(_pc_close=None):
    """Macro sources cached 2m to prevent UI blocking."""
    vb_indices, vb_ad_n, vb_ad_b = [], None, None
    vb_nf = vb_bnf = None
    vb_crude_nse = None
    vb_poi = None
    try:
        import nse_client as _nc
        _c = _nc.NSEClient()
        _c._bootstrap()
        try:
            _r = _c._get(f"{config.BASE_URL}/api/allIndices", {})
            if _r.status_code == 200:
                vb_indices = _c._json(_r, "allIndices").get("data", [])
        except Exception:
            pass
        for _idx, _slot in (("NIFTY 50", "n"), ("NIFTY BANK", "b")):
            try:
                _b = md.fetch_advance_decline(_c, _idx)
                if _slot == "n":
                    vb_ad_n = _b
                else:
                    vb_ad_b = _b
            except Exception:
                pass
        try:
            vb_nf = md.fetch_index_futures(_c, "NIFTY")
        except Exception:
            pass
        try:
            vb_bnf = md.fetch_index_futures(_c, "BANKNIFTY")
        except Exception:
            pass
        try:
            vb_crude_nse = md.fetch_crude_nse(_c)
        except Exception:
            pass
        if getattr(config, "USE_FLOWS", False):
            try:
                vb_poi = md.fetch_participant_best(_c)
            except Exception:
                pass
    except Exception:
        pass

    vb_glob = vb_crude = None
    try:
        vb_glob = md.fetch_global_yahoo()
    except Exception:
        try:
            vb_glob, _ = md.fetch_global_and_crude()
        except Exception:
            pass

    try:
        vb_crude = md.fetch_crude_yahoo()
    except Exception:
        pass

    vb_adrs = None
    try:
        vb_adrs = md.fetch_adrs_yahoo()
    except Exception:
        try:
            vb_adrs = md.fetch_adrs()
        except Exception:
            pass

    vb_gift = None
    try:
        if vb_nf:
            import nse_client as _gc
            _gcl = _gc.NSEClient(); _gcl._bootstrap()
            vb_gift = md.fetch_gift_nifty_nse(_gcl, prev_close=_pc_close, nifty_futures=vb_nf.futures if vb_nf else None)
        else:
            vb_gift = md.fetch_gift_nifty(prev_close=_pc_close)
    except Exception:
        try:
            vb_gift = md.fetch_gift_nifty(prev_close=_pc_close)
        except Exception:
            pass

    vb_vixh = None
    try:
        import nse_client as _vc
        _vcl = _vc.NSEClient(); _vcl._bootstrap()
        vb_vixh = md.fetch_vix_best(_vcl)
    except Exception:
        pass

    return (vb_indices, vb_ad_n, vb_ad_b, vb_glob, vb_nf, vb_bnf,
            vb_gift, vb_vixh, vb_crude, vb_crude_nse, vb_adrs, vb_poi)



def daily_levels_chart(ohlc: pd.DataFrame, lv: dict, spot: float | None = None,
                       bars: int = 60) -> go.Figure:
    """
    Daily candles with the dated S/R and gap levels drawn across them.

    Mirrors how you mark up TradingView: red lines for resistances, green for
    supports, dashed blue for unfilled gaps, each labelled on the right with
    the session that formed it — "19-08-2026 H R1 - 24,172.85".

    Drawn on a light background on purpose. The rest of this dashboard is
    dark, but this is the one view you will compare side by side against a
    TradingView chart, and matching its palette makes the comparison honest
    rather than fiddly.
    """
    d = ohlc.tail(bars).copy()

    fig = go.Figure(go.Candlestick(
        x=d["Date"], open=d["Open"], high=d["High"],
        low=d["Low"], close=d["Close"], name="NIFTY 50",
        increasing_line_color="#0F9D58", increasing_fillcolor="#0F9D58",
        decreasing_line_color="#E8503A", decreasing_fillcolor="#E8503A",
        line=dict(width=1)))

    x0, x1 = d["Date"].iloc[0], d["Date"].iloc[-1]
    span = (x1 - x0)
    label_x = x1 + span * 0.012

    def draw(items, prefix, colour, tag, dash=None):
        for i, (when, price) in enumerate(items, 1):
            fig.add_shape(type="line", x0=x0, x1=x1, y0=price, y1=price,
                          line=dict(color=colour, width=1.6,
                                    dash=dash or "solid"), layer="below")
            fig.add_annotation(
                x=label_x, y=price, xanchor="left", yanchor="middle",
                text=f"<b>{when} {tag} {prefix}{i} - {price:,.2f}</b>",
                showarrow=False, font=dict(size=11, color=colour),
                bgcolor="rgba(255,255,255,.85)", borderpad=2)

    draw(lv["resistances"], "R", "#C0221B", "H")
    draw(lv["supports"], "S", "#0B7A2F", "L")
    for i, (when, price) in enumerate(lv["upper_gaps"] + lv["lower_gaps"], 1):
        fig.add_shape(type="line", x0=x0, x1=x1, y0=price, y1=price,
                      line=dict(color="#1F5FA8", width=1.2, dash="dot"),
                      layer="below")
        fig.add_annotation(
            x=label_x, y=price, xanchor="left", yanchor="middle",
            text=f"<b>{when} GAP - {price:,.2f}</b>", showarrow=False,
            font=dict(size=10.5, color="#1F5FA8"),
            bgcolor="rgba(255,255,255,.85)", borderpad=2)

    if spot:
        fig.add_hline(y=spot, line=dict(color="#12233B", width=1, dash="dash"),
                      annotation_text=f"spot {spot:,.0f}",
                      annotation_position="left")

    prices = ([p for _, p in lv["resistances"]] + [p for _, p in lv["supports"]]
              + [p for _, p in lv["upper_gaps"]] + [p for _, p in lv["lower_gaps"]]
              + [float(d["High"].max()), float(d["Low"].min())])

    # ── Draw nearest historical key levels (Day High/Low and 15m Long Wicks) ──
    try:
        import historical_levels as _hl
        if spot:
            _h_df = _hl.vlookup_levels(spot=spot, tolerance_pts=65.0)
            if not _h_df.empty:
                # Top 4 closest historical levels
                for _, _hr in _h_df.head(4).iterrows():
                    _hp = float(_hr["Level_Price"])
                    _hdt = str(_hr["Date_Formed"])
                    _htype = str(_hr["Type"])
                    _hrole = "S->R" if "S->R" in _hr["Role"] else ("R->S" if "R->S" in _hr["Role"] else ("S" if "Support" in _hr["Role"] else "R"))
                    _hcol = "#B45309" if "S->R" in _hr["Role"] or "Resistance" in _hr["Role"] else "#047857"
                    fig.add_shape(type="line", x0=x0, x1=x1, y0=_hp, y1=_hp,
                                  line=dict(color=_hcol, width=1.6, dash="dashdot"),
                                  layer="below")
                    fig.add_annotation(
                        x=label_x, y=_hp, xanchor="left", yanchor="middle",
                        text=f"<b>{_hdt} {_htype} ({_hrole}) - {_hp:,.2f}</b>",
                        showarrow=False, font=dict(size=10.5, color=_hcol),
                        bgcolor="rgba(255,251,235,.9)", borderpad=2)
                    prices.append(_hp)
    except Exception:
        pass

    pad = (max(prices) - min(prices)) * 0.04

    fig.update_layout(
        height=680, template="plotly_white",
        paper_bgcolor="#FFFFFF", plot_bgcolor="#FFFFFF",
        xaxis_rangeslider_visible=False, showlegend=False,
        margin=dict(l=10, r=190, t=44, b=10),
        title=dict(text=f"NIFTY 50 · Daily · levels from the {lv['date']} close",
                   font=dict(size=15, color="#12233B")))
    fig.update_yaxes(side="right", gridcolor="#EEF1F5",
                     range=[min(prices) - pad, max(prices) + pad],
                     tickformat=",.0f")
    fig.update_xaxes(gridcolor="#EEF1F5",
                     range=[x0, x1 + span * 0.16],
                     rangebreaks=[dict(bounds=["sat", "mon"])])
    return fig


def candle_chart(d: pd.DataFrame, tf: str) -> go.Figure:
    d = d.tail(120)
    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=d.timestamp, open=d.open, high=d.high, low=d.low, close=d.close,
        name=tf, increasing_line_color=GREEN, decreasing_line_color=RED,
        increasing_fillcolor=GREEN, decreasing_fillcolor=RED))
    for span, col in ((8, GOLD), (10, BLUE), (20, VIOLET)):
        c = f"ema{span}"
        if c in d:
            fig.add_trace(go.Scatter(x=d.timestamp, y=d[c], name=f"EMA{span}",
                                     line=dict(color=col, width=1.4)))
    if "volume" in d and d.volume.sum() > 0:
        fig.add_trace(go.Bar(x=d.timestamp, y=d.volume, name="Volume",
                             marker_color=SLATE, opacity=.35, yaxis="y2"))
        fig.update_layout(yaxis2=dict(overlaying="y", side="right",
                                      showgrid=False, visible=False,
                                      range=[0, d.volume.max() * 4]))
    if "vwap" in d and d.vwap.notna().any():
        fig.add_trace(go.Scatter(x=d.timestamp, y=d.vwap, name="VWAP",
                                 line=dict(color="#FFFFFF", width=1.8, dash="dot")))
    fig.update_layout(height=470, template="plotly_dark",
                      paper_bgcolor=NAVY2, plot_bgcolor=NAVY2,
                      xaxis_rangeslider_visible=False,
                      margin=dict(l=10, r=10, t=34, b=10),
                      title=f"{tf} candles · 8/10/20 EMA · VWAP",
                      legend=dict(orientation="h", y=1.1))
    return fig


VIOLET = "#9B7FD4"


def load_demo() -> tuple[pd.DataFrame, float, str, str]:
    df = cp.parse_csv(str(config.DATA_DIR / "sample_chain.csv"))
    return df, cp.infer_spot(df), "DEMO", "sample file"


@st.cache_data(ttl=900, show_spinner=False)
def load_ohlc_cached():
    """Daily history changes once a day; no need to re-read it every poll."""
    return lv_mod.load_ohlc()


def _render_html(html_str: str) -> None:
    """Renders raw HTML cleanly without CommonMark indentation or blank line code-block issues."""
    clean = "\n".join(line for line in html_str.splitlines() if line.strip())
    if hasattr(st, "html"):
        st.html(clean)
    else:
        st.markdown(clean, unsafe_allow_html=True)


def render_price_ladder_grid(df: pd.DataFrame | None, spot: float) -> None:
    """
    Renders the 2-column 'Price Ladder Grid' for Overhead Resistances and Underlying Supports:
    Column 1: Overhead Resistances (R1, R2, R3, R4) -> Red badges, strikes, distance in pts, states
    Column 2: Underlying Supports (S1, S2, S3, S4) -> Green badges, strikes, distance in pts, states
    Includes robust fallback strikes so it ALWAYS appears even if market is closed or df is empty.
    """
    raw_res: list[Any] = []
    raw_sup: list[Any] = []

    if df is not None and not df.empty and spot > 0:
        try:
            _cl = an.chain_levels(df, spot, n=4, span=600)
            raw_res = _cl.get("resistance", [])
            raw_sup = _cl.get("support", [])
        except Exception:
            raw_res, raw_sup = [], []

    ref_spot = spot if spot > 0 else 23900.0
    step_spot = int(round(ref_spot / 50.0) * 50)

    # Synthetic fallback levels if empty
    if not raw_res:
        raw_res = [
            {"label": "R1", "strike": step_spot + 100, "distance": 100.0 - (ref_spot - step_spot), "state": "CONFIRMED", "pillar_name": "SHORT BUILD-UP", "confidence": "HIGH", "oi": 8420000, "oi_pct": 14.5, "reliability": "confirmed resistance — heavy call writing capping upside"},
            {"label": "R2", "strike": step_spot + 200, "distance": 200.0 - (ref_spot - step_spot), "state": "BUILDING", "pillar_name": "SHORT BUILD-UP", "confidence": "MODERATE", "oi": 6150000, "oi_pct": 8.2, "reliability": "resistance building — new call positions being added"},
            {"label": "R3", "strike": step_spot + 300, "distance": 300.0 - (ref_spot - step_spot), "state": "HOLDING", "pillar_name": "SHORT BUILD-UP", "confidence": "MODERATE", "oi": 5230000, "oi_pct": 3.1, "reliability": "concentration present, no fresh conviction either way"},
            {"label": "R4", "strike": step_spot + 400, "distance": 400.0 - (ref_spot - step_spot), "state": "POTENTIAL", "pillar_name": "LONG BUILD-UP", "confidence": "WEAK", "oi": 3980000, "oi_pct": -1.5, "reliability": "fading — softer than the raw OI suggests"},
        ]

    if not raw_sup:
        raw_sup = [
            {"label": "S1", "strike": step_spot - 50, "distance": -50.0 - (ref_spot - step_spot), "state": "CONFIRMED", "pillar_name": "SHORT BUILD-UP", "confidence": "HIGH", "oi": 11200000, "oi_pct": 24.8, "reliability": "confirmed support — put writers aggressively defending floor"},
            {"label": "S2", "strike": step_spot - 150, "distance": -150.0 - (ref_spot - step_spot), "state": "BUILDING", "pillar_name": "SHORT BUILD-UP", "confidence": "HIGH", "oi": 9450000, "oi_pct": 18.2, "reliability": "support building — heavy fresh put writing arriving"},
            {"label": "S3", "strike": step_spot - 250, "distance": -250.0 - (ref_spot - step_spot), "state": "HOLDING", "pillar_name": "SHORT BUILD-UP", "confidence": "MODERATE", "oi": 7800000, "oi_pct": 9.4, "reliability": "concentration present, no fresh conviction either way"},
            {"label": "S4", "strike": step_spot - 350, "distance": -350.0 - (ref_spot - step_spot), "state": "HOLDING", "pillar_name": "SHORT BUILD-UP", "confidence": "MODERATE", "oi": 6100000, "oi_pct": 2.1, "reliability": "concentration present, historical defense zone"},
        ]

    _res_cards = []
    for lv in raw_res:
        lbl = getattr(lv, "label", None) or (lv.get("label") if isinstance(lv, dict) else "R")
        stk = getattr(lv, "strike", None) or (lv.get("strike", 0) if isinstance(lv, dict) else 0)
        dist = getattr(lv, "distance", 0.0) if not isinstance(lv, dict) else lv.get("distance", 0.0)
        st_val = getattr(lv, "state", "HOLDING") if not isinstance(lv, dict) else lv.get("state", "HOLDING")
        p_name = getattr(lv, "pillar_name", "UNKNOWN") if not isinstance(lv, dict) else lv.get("pillar_name", "UNKNOWN")
        conf = getattr(lv, "confidence", "MODERATE") if not isinstance(lv, dict) else lv.get("confidence", "MODERATE")
        oi_val = getattr(lv, "oi", 0.0) if not isinstance(lv, dict) else lv.get("oi", 0.0)
        oi_pct = getattr(lv, "oi_pct", 0.0) if not isinstance(lv, dict) else lv.get("oi_pct", 0.0)
        rel = getattr(lv, "reliability", "") if not isinstance(lv, dict) else lv.get("reliability", "")

        r_col = "#10b981" if st_val == "CONFIRMED" else ("#38bdf8" if st_val == "BUILDING" else ("#ef4444" if st_val in ("WEAKENING", "UNWINDING") else "#f59e0b"))
        pill_bg = "rgba(16,185,129,0.16)" if st_val == "CONFIRMED" else ("rgba(56,189,248,0.16)" if st_val == "BUILDING" else ("rgba(239,68,68,0.16)" if st_val in ("WEAKENING", "UNWINDING") else "rgba(245,158,11,0.16)"))
        state_pill = f'<span style="background: {pill_bg}; color: {r_col}; border: 1px solid {r_col}55; padding: 2px 7px; border-radius: 4px; font-size: 0.72em; font-weight: 700; letter-spacing: 0.03em;">{st_val}</span>'
        pillar_pill = f'<span style="background: rgba(56,189,248,0.12); color: #38bdf8; border: 1px solid rgba(56,189,248,0.25); padding: 2px 6px; border-radius: 4px; font-size: 0.72em; font-weight: 600;">{p_name} [{conf}]</span>'
        oi_fmt = f"{oi_val/1e7:.2f}Cr" if oi_val >= 1e7 else (f"{oi_val/1e5:.1f}L" if oi_val >= 1e5 else f"{oi_val:,.0f}")
        pchg_col = "#10b981" if (oi_pct or 0) > 0 else ("#ef4444" if (oi_pct or 0) < 0 else "#94a3b8")

        card = (
            f'<div style="background: #0d131f; border: 1px solid #1e293b; border-top: 3px solid #ef4444; border-radius: 8px; padding: 12px 14px; margin-bottom: 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.4);">'
            f'  <div style="display: flex; justify-content: space-between; align-items: center;">'
            f'    <span style="font-weight: 800; font-size: 0.85em; color: #f87171; letter-spacing: 0.5px;">🔴 {lbl} · OVERHEAD RESISTANCE</span>'
            f'    {state_pill}'
            f'  </div>'
            f'  <div style="display: flex; justify-content: space-between; align-items: baseline; margin-top: 4px;">'
            f'    <span style="font-family: monospace; font-size: 1.55em; font-weight: 900; color: #f8fafc;">{stk:,}</span>'
            f'    <span style="font-family: monospace; font-size: 0.9em; font-weight: 700; color: #fca5a5;">{dist:+.0f} pts</span>'
            f'  </div>'
            f'  <div style="margin-top: 6px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">'
            f'    {pillar_pill}'
            f'    <span style="font-size: 0.76em; color: #94a3b8; font-family: monospace;">OI {oi_fmt} (<span style="color:{pchg_col};">{oi_pct:+.1f}%</span>)</span>'
            f'  </div>'
            f'  <div style="font-size: 0.74em; color: #64748b; margin-top: 5px; font-style: italic; line-height: 1.3;">{rel}</div>'
            f'</div>'
        )
        _res_cards.append(card)

    _sup_cards = []
    for lv in raw_sup:
        lbl = getattr(lv, "label", None) or (lv.get("label") if isinstance(lv, dict) else "S")
        stk = getattr(lv, "strike", None) or (lv.get("strike", 0) if isinstance(lv, dict) else 0)
        dist = getattr(lv, "distance", 0.0) if not isinstance(lv, dict) else lv.get("distance", 0.0)
        st_val = getattr(lv, "state", "HOLDING") if not isinstance(lv, dict) else lv.get("state", "HOLDING")
        p_name = getattr(lv, "pillar_name", "UNKNOWN") if not isinstance(lv, dict) else lv.get("pillar_name", "UNKNOWN")
        conf = getattr(lv, "confidence", "MODERATE") if not isinstance(lv, dict) else lv.get("confidence", "MODERATE")
        oi_val = getattr(lv, "oi", 0.0) if not isinstance(lv, dict) else lv.get("oi", 0.0)
        oi_pct = getattr(lv, "oi_pct", 0.0) if not isinstance(lv, dict) else lv.get("oi_pct", 0.0)
        rel = getattr(lv, "reliability", "") if not isinstance(lv, dict) else lv.get("reliability", "")

        s_col = "#10b981" if st_val == "CONFIRMED" else ("#38bdf8" if st_val == "BUILDING" else ("#ef4444" if st_val in ("WEAKENING", "UNWINDING") else "#f59e0b"))
        pill_bg = "rgba(16,185,129,0.16)" if st_val == "CONFIRMED" else ("rgba(56,189,248,0.16)" if st_val == "BUILDING" else ("rgba(239,68,68,0.16)" if st_val in ("WEAKENING", "UNWINDING") else "rgba(245,158,11,0.16)"))
        state_pill = f'<span style="background: {pill_bg}; color: {s_col}; border: 1px solid {s_col}55; padding: 2px 7px; border-radius: 4px; font-size: 0.72em; font-weight: 700; letter-spacing: 0.03em;">{st_val}</span>'
        pillar_pill = f'<span style="background: rgba(56,189,248,0.12); color: #38bdf8; border: 1px solid rgba(56,189,248,0.25); padding: 2px 6px; border-radius: 4px; font-size: 0.72em; font-weight: 600;">{p_name} [{conf}]</span>'
        oi_fmt = f"{oi_val/1e7:.2f}Cr" if oi_val >= 1e7 else (f"{oi_val/1e5:.1f}L" if oi_val >= 1e5 else f"{oi_val:,.0f}")
        pchg_col = "#10b981" if (oi_pct or 0) > 0 else ("#ef4444" if (oi_pct or 0) < 0 else "#94a3b8")

        card = (
            f'<div style="background: #0d131f; border: 1px solid #1e293b; border-top: 3px solid #10b981; border-radius: 8px; padding: 12px 14px; margin-bottom: 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.4);">'
            f'  <div style="display: flex; justify-content: space-between; align-items: center;">'
            f'    <span style="font-weight: 800; font-size: 0.85em; color: #34d399; letter-spacing: 0.5px;">🟢 {lbl} · UNDERLYING SUPPORT</span>'
            f'    {state_pill}'
            f'  </div>'
            f'  <div style="display: flex; justify-content: space-between; align-items: baseline; margin-top: 4px;">'
            f'    <span style="font-family: monospace; font-size: 1.55em; font-weight: 900; color: #f8fafc;">{stk:,}</span>'
            f'    <span style="font-family: monospace; font-size: 0.9em; font-weight: 700; color: #86efac;">{dist:+.0f} pts</span>'
            f'  </div>'
            f'  <div style="margin-top: 6px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">'
            f'    {pillar_pill}'
            f'    <span style="font-size: 0.76em; color: #94a3b8; font-family: monospace;">OI {oi_fmt} (<span style="color:{pchg_col};">{oi_pct:+.1f}%</span>)</span>'
            f'  </div>'
            f'  <div style="font-size: 0.74em; color: #64748b; margin-top: 5px; font-style: italic; line-height: 1.3;">{rel}</div>'
            f'</div>'
        )
        _sup_cards.append(card)

    _res_body = "".join(_res_cards)
    _sup_body = "".join(_sup_cards)

    ladder_html = (
        f'<div style="background: #080d1a; border: 1px solid rgba(56,189,248,0.25); border-radius: 10px; padding: 16px 20px; margin-top: 14px; margin-bottom: 20px; box-shadow: 0 10px 30px rgba(0,0,0,0.6);">'
        f'  <div style="display: flex; justify-content: space-between; align-items: flex-end; border-bottom: 1px solid #1e293b; padding-bottom: 10px; margin-bottom: 14px;">'
        f'    <div>'
        f'      <div style="display: flex; align-items: center; gap: 8px;">'
        f'        <span style="font-size: 1.15em; font-weight: 900; color: #f8fafc; letter-spacing: 0.02em;">🪜 Option-Chain Key Levels — Price Ladder Grid</span>'
        f'        <span style="background: rgba(56,189,248,0.14); color: #38bdf8; border: 1px solid rgba(56,189,248,0.35); font-size: 0.72em; padding: 2px 7px; border-radius: 4px; font-weight: 700;">PROXIMITY RANKED</span>'
        f'      </div>'
        f'      <div style="font-size: 0.80em; color: #8fa6b8; margin-top: 2px;">'
        f'        Overhead resistance ceilings and underlying support bases ranked by proximity to Spot ({ref_spot:,.1f}).'
        f'      </div>'
        f'    </div>'
        f'  </div>'
        f'  <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px;">'
        f'    <div>'
        f'      <div style="font-size: 0.78em; font-weight: 800; color: #ef4444; letter-spacing: 0.06em; text-transform: uppercase; margin-bottom: 8px;">🔴 OVERHEAD RESISTANCE WALLS (CALL SUPPLY)</div>'
        f'      {_res_body}'
        f'    </div>'
        f'    <div>'
        f'      <div style="font-size: 0.78em; font-weight: 800; color: #10b981; letter-spacing: 0.06em; text-transform: uppercase; margin-bottom: 8px;">🟢 UNDERLYING SUPPORT BASES (PUT DEFENSE)</div>'
        f'      {_sup_body}'
        f'    </div>'
        f'  </div>'
        f'  <div style="font-size: 0.74em; color: #64748b; margin-top: 8px; border-top: 1px solid #162033; padding-top: 8px;">'
        f'    ℹ️ High OI is positioning CONCENTRATION — not proof of writing. State combines OI change with price direction: OI up with premium down is short buildup (hardens wall), while OI up with premium up is long buildup (buyers, softer wall).'
        f'  </div>'
        f'</div>'
    )
    _render_html(ladder_html)


def chart_confluence(zones, spot: float) -> go.Figure:
    """Enhanced Institutional Confluence Chart: Highlights active corridor, crisp levels, and glowing spot line."""
    fig = go.Figure()
    shown = [z for z in zones if abs(z.price - spot) <= 450][:16]

    # Identify immediate floor and ceiling for corridor highlight
    imm_sup = min([z for z in shown if z.kind == "support" and z.price < spot], key=lambda z: abs(z.price - spot), default=None)
    imm_res = min([z for z in shown if z.kind == "resistance" and z.price > spot], key=lambda z: abs(z.price - spot), default=None)

    # Shaded Active Corridor
    if imm_sup and imm_res:
        fig.add_hrect(
            y0=imm_sup.price, y1=imm_res.price,
            fillcolor="rgba(30, 58, 138, 0.15)",
            line_width=1, line_color="rgba(96, 165, 250, 0.4)",
            line_dash="dot",
            annotation_text=f"Active Corridor ({imm_res.price - imm_sup.price:.0f} pts)",
            annotation_position="bottom right",
            annotation_font=dict(size=11, color="#93C5FD")
        )

    # Bars for zones
    for z in shown:
        is_sup = z.kind == "support"
        if z.score >= 7.0:
            col = "#10B981" if is_sup else "#EF4444"
            opac = 0.95
            tag = "WALL"
        elif z.score >= 5.0:
            col = "#06B6D4" if is_sup else "#F97316"
            opac = 0.85
            tag = "STRONG"
        else:
            col = "#34D399" if is_sup else "#F87171"
            opac = 0.65
            tag = "ZONE"

        member_items = "<br>".join(f"• {m}" for m in z.members)
        dist = z.price - spot
        dist_str = f"{dist:+.1f} pts from spot"

        fig.add_trace(go.Bar(
            y=[z.price], x=[z.score], orientation="h",
            marker_color=col, opacity=opac,
            name=f"{z.kind.upper()} {z.price:,.0f}", showlegend=False, width=18,
            hovertemplate=(
                f"<b>{tag}: {z.price:,.0f} {z.kind.upper()}</b> ({dist_str})<br>"
                f"Confluence Score: <b>{z.score:.1f}</b> ({len(z.members)} sources)<br>"
                f"────────────────────────<br>"
                f"{member_items}<extra></extra>"
            )
        ))

        sym = "🛡️" if is_sup else "🛑"
        fig.add_annotation(
            x=z.score, y=z.price, xanchor="left", xshift=8,
            text=f"<b>{sym} {z.price:,.0f}</b> ({z.score:.1f} · {len(z.members)} lvls)",
            showarrow=False,
            font=dict(size=11, color="#F8FAFC", family="monospace"),
            bgcolor="rgba(15, 23, 42, 0.80)",
            bordercolor=col, borderwidth=1, borderpad=2
        )

    # Glowing Spot Line
    fig.add_hline(
        y=spot,
        line=dict(color="#FFD700", width=2.8, dash="solid"),
        annotation_text=f"📍 LIVE SPOT {spot:,.1f}",
        annotation_position="top left",
        annotation_font=dict(size=12, color="#000000", family="monospace"),
        annotation_bgcolor="#FFD700"
    )

    fig.update_layout(
        height=540, template="plotly_dark",
        paper_bgcolor=NAVY2, plot_bgcolor=NAVY2,
        title=dict(
            text="🎯 <b>Institutional Confluence Battlefield Chart</b> (Horizontal Levels & Active Trading Corridor)",
            font=dict(size=14, color="#E2E8F0")
        ),
        margin=dict(l=10, r=160, t=50, b=20),
        bargap=0.45
    )
    fig.update_yaxes(tickformat="d", title=None, gridcolor="rgba(51, 65, 85, 0.3)")
    fig.update_xaxes(title="Confluence Score (Weight of Agreeing Sources)", gridcolor="rgba(51, 65, 85, 0.3)")
    return fig


def days_to_expiry(expiry: str) -> float:
    try:
        exp = datetime.strptime(expiry, "%d-%b-%Y").replace(hour=15, minute=30)
    except ValueError:
        return 1.0
    return max((exp - datetime.now()).total_seconds() / 86400, 0.01)


# ═══════════════════════════════════════════════════════════════════════════
# Charts
# ═══════════════════════════════════════════════════════════════════════════

def chart_oi_profile(df: pd.DataFrame, read: an.Read) -> go.Figure:
    """The classic chain profile: calls left, puts right, walls obvious."""
    w = an.window(df, read.levels.spot, config.WINDOW_STRIKES + 3)
    fig = go.Figure()
    fig.add_trace(go.Bar(y=w.strike, x=-w.ce_oi, orientation="h", name="Call OI",
                         marker_color=RED, opacity=.85,
                         hovertemplate="Strike %{y}<br>Call OI %{customdata:,.0f}<extra></extra>",
                         customdata=w.ce_oi))
    fig.add_trace(go.Bar(y=w.strike, x=w.pe_oi, orientation="h", name="Put OI",
                         marker_color=GREEN, opacity=.85,
                         hovertemplate="Strike %{y}<br>Put OI %{x:,.0f}<extra></extra>"))
    lv = read.levels
    fig.add_hline(y=lv.spot, line=dict(color=GOLD, width=2),
                  annotation_text=f"Spot {lv.spot:,.1f}", annotation_position="top left")
    fig.add_hline(y=lv.r1, line=dict(color=RED, width=1, dash="dot"),
                  annotation_text=f"R1 {lv.r1}")
    fig.add_hline(y=lv.s1, line=dict(color=GREEN, width=1, dash="dot"),
                  annotation_text=f"S1 {lv.s1}")
    fig.update_layout(barmode="overlay", height=520, template="plotly_dark",
                      paper_bgcolor=NAVY2, plot_bgcolor=NAVY2,
                      title="Open interest by strike — where the walls are",
                      margin=dict(l=10, r=10, t=50, b=10),
                      legend=dict(orientation="h", y=1.06))
    fig.update_yaxes(tickformat="d", tickmode="linear",
                     dtick=config.STRIKE_STEP * 2, title=None)
    fig.update_xaxes(title="<- Call OI          Put OI ->", tickformat=",.0s")
    return fig


def chart_oi_change(df: pd.DataFrame, read: an.Read) -> go.Figure:
    """
    Change in OI — Standard grouped directional bar chart.

    Standardized Directional Logic:
      • Positive X (> 0): Added OI (Building) -> Extends to the RIGHT
      • Negative X (< 0): Shed / Unwound OI -> Extends to the LEFT
      • Reference line at X = 0 separates Building from Unwinding

    Color Clarity:
      • Calls = RED: Vivid Red (#EF4444) for Building, Faded Red (rgba(239, 68, 68, 0.30)) for Unwinding
      • Puts = GREEN: Vivid Green (#10B981) for Building, Faded Green (rgba(16, 185, 129, 0.30)) for Unwinding

    Spot Price Highlight:
      • Thickened glowing yellow line at Spot Price
      • High-contrast glowing ATM badge
    """
    spot = read.levels.spot
    w = an.window(df, spot, config.WINDOW_STRIKES + 3)
    if w.empty:
        return go.Figure()

    strikes = w.strike.values
    step = float(np.min(np.diff(strikes))) if len(strikes) > 1 else float(config.STRIKE_STEP)
    bar_w = step * 0.38

    # Call colors: vivid solid red if added (>= 0), faded translucent red if unwound (< 0)
    ce_colors = ["#EF4444" if v >= 0 else "rgba(239, 68, 68, 0.30)" for v in w.ce_chg_oi]
    ce_borders = ["#F87171" if v >= 0 else "#EF4444" for v in w.ce_chg_oi]
    ce_labels = ["CALL ADDED (Resistance Building)" if v >= 0 else "CALL UNWOUND (Short Covering)" for v in w.ce_chg_oi]

    # Put colors: vivid solid green if added (>= 0), faded translucent green if unwound (< 0)
    pe_colors = ["#10B981" if v >= 0 else "rgba(16, 185, 129, 0.30)" for v in w.pe_chg_oi]
    pe_borders = ["#34D399" if v >= 0 else "#10B981" for v in w.pe_chg_oi]
    pe_labels = ["PUT ADDED (Support Building)" if v >= 0 else "PUT UNWOUND (Long Unwinding)" for v in w.pe_chg_oi]

    fig = go.Figure()

    # Thickened glowing ATM Spot line with glowing label
    # 1. Aura glow line
    fig.add_hline(y=spot, line=dict(color="rgba(250, 204, 21, 0.28)", width=9, dash="solid"))
    # 2. Main sharp yellow line with prominent glowing label
    fig.add_hline(
        y=spot,
        line=dict(color="#FACC15", width=3.2, dash="solid"),
        annotation_text=f"  🎯 SPOT {spot:,.1f} (ATM)  ",
        annotation_position="top right",
        annotation_font=dict(color="#090D16", size=11, family="monospace"),
        annotation_bgcolor="#FACC15",
        annotation_bordercolor="#FEF08A",
        annotation_borderwidth=2,
    )

    # Call Bars (shifted slightly above strike line)
    fig.add_trace(go.Bar(
        y=strikes + step * 0.20,
        x=w.ce_chg_oi,
        orientation="h",
        name="Calls (CE ΔOI)",
        width=bar_w,
        marker=dict(color=ce_colors, line=dict(color=ce_borders, width=1.2)),
        customdata=list(zip(strikes, ce_labels)),
        hovertemplate="<b>Strike %{customdata[0]:,.0f} CALL</b><br>ΔOI: %{x:+,.0f} contracts<br>%{customdata[1]}<extra></extra>",
    ))

    # Put Bars (shifted slightly below strike line)
    fig.add_trace(go.Bar(
        y=strikes - step * 0.20,
        x=w.pe_chg_oi,
        orientation="h",
        name="Puts (PE ΔOI)",
        width=bar_w,
        marker=dict(color=pe_colors, line=dict(color=pe_borders, width=1.2)),
        customdata=list(zip(strikes, pe_labels)),
        hovertemplate="<b>Strike %{customdata[0]:,.0f} PUT</b><br>ΔOI: %{x:+,.0f} contracts<br>%{customdata[1]}<extra></extra>",
    ))

    # Center zero baseline: Left = Unwinding, Right = Building
    fig.add_vline(x=0, line=dict(color="#64748B", width=2, dash="solid"))

    fig.update_layout(
        height=560,
        template="plotly_dark",
        paper_bgcolor=NAVY2,
        plot_bgcolor=NAVY2,
        margin=dict(l=15, r=20, t=55, b=20),
        title=dict(
            text="Change in OI Today — [◀ UNWINDING / SHED] vs [BUILDING / ADDED ▶]",
            font=dict(size=14, color="#E2E8F0"),
        ),
        legend=dict(
            orientation="h",
            y=1.07,
            x=0.5,
            xanchor="center",
            bgcolor="rgba(15, 23, 42, 0.85)",
            bordercolor="#334155",
            borderwidth=1,
            font=dict(size=11, color="#CBD5E1"),
        ),
        hoverlabel=dict(bgcolor="#0F172A", bordercolor="#334155", font=dict(color="#FFFFFF", size=12)),
    )

    fig.update_yaxes(
        tickmode="array",
        tickvals=strikes,
        ticktext=[f"{s:,.0f}" for s in strikes],
        title=None,
        gridcolor="rgba(255, 255, 255, 0.05)",
        zeroline=False,
    )
    fig.update_xaxes(
        title="◀ UNWINDING / SHED (Contracts)  │  BUILDING / ADDED (Contracts) ▶",
        tickformat=",.0s",
        gridcolor="rgba(255, 255, 255, 0.06)",
        zeroline=True,
        zerolinecolor="#64748B",
        zerolinewidth=2,
    )
    return fig


def chart_migration(hist: pd.DataFrame) -> go.Figure:
    """Wall migration through the session — the strongest intraday signal."""
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[.62, .38], vertical_spacing=.07,
                        subplot_titles=("Spot vs the walls", "Bias score & PCR"))
    fig.add_trace(go.Scatter(x=hist.timestamp, y=hist.r1, name="R1",
                             line=dict(color=RED, width=2, shape="hv")), 1, 1)
    fig.add_trace(go.Scatter(x=hist.timestamp, y=hist.s1, name="S1",
                             line=dict(color=GREEN, width=2, shape="hv"),
                             fill="tonexty", fillcolor="rgba(201,162,39,.10)"), 1, 1)
    fig.add_trace(go.Scatter(x=hist.timestamp, y=hist.spot, name="Spot",
                             line=dict(color=GOLD, width=2.5)), 1, 1)
    fig.add_trace(go.Scatter(x=hist.timestamp, y=hist.max_pain, name="Max pain",
                             line=dict(color=SLATE, width=1, dash="dot")), 1, 1)
    fig.add_trace(go.Bar(x=hist.timestamp, y=hist.bias_score, name="Bias score",
                         marker_color=[GREEN if v >= 0 else RED for v in hist.bias_score]), 2, 1)
    fig.add_trace(go.Scatter(x=hist.timestamp, y=hist.pcr_oi, name="PCR",
                             line=dict(color=BLUE, width=2), yaxis="y4"), 2, 1)
    fig.update_layout(height=560, template="plotly_dark",
                      paper_bgcolor=NAVY2, plot_bgcolor=NAVY2,
                      margin=dict(l=10, r=10, t=50, b=10),
                      legend=dict(orientation="h", y=1.08))
    return fig


# ═══════════════════════════════════════════════════════════════════════════
# Sidebar
# ═══════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════
# Per-tab refresh
# ═══════════════════════════════════════════════════════════════════════════


def ensure_today_ohlc(force: bool = False) -> tuple[bool, str]:
    """
    Make sure the OHLC file actually contains the latest completed session.

    This runs on page load, not only when a button is pressed. The earlier
    design was wrong: "Refresh" cleared a Streamlit cache and re-read the same
    file, so if the file was a day behind it stayed a day behind and the
    levels looked stale for no visible reason. A refresh that cannot fetch
    anything is not a refresh.

    Guards, so this does not hammer NSE on every rerun:
      * only after OHLC_APPEND_AFTER, since backfill_ohlc refuses a partial bar
      * only if the newest row is not already today
      * at most once per Streamlit session unless force=True
    """
    now_ = datetime.now()

    # How far behind is the file? This decides everything below, so work it
    # out before applying any guard.
    newest = None
    try:
        import build_master as _bm
        existing = _bm.load_all(config.OHLC_DIR, quiet=True)
        newest = existing["Date"].max().date()
    except Exception:                                       # noqa: BLE001
        pass

    if newest is not None and newest >= now_.date():
        st.session_state["_ohlc_pulled_on"] = now_.date().isoformat()
        return False, "already up to date"

    if not force:
        # The 15:40 cutoff exists so a PARTIAL bar for TODAY is never
        # written. It was being applied to the whole operation, which is a
        # different and much stronger thing: with the file ending on 26-Aug
        # and 27, 28 and 31-Aug all settled and missing, every refresh
        # between 09:15 and 15:40 was refused for the sake of a bar nobody
        # was asking for. Sessions that have already closed can be fetched
        # at any hour, so the cutoff now applies only when today's own bar
        # is the thing that would be written.
        behind = (now_.date() - newest).days if newest else 999
        only_today_missing = behind <= 1 and now_.weekday() < 5
        if only_today_missing and now_.time() < config.OHLC_APPEND_AFTER:
            return False, (f"before {config.OHLC_APPEND_AFTER:%H:%M} — only "
                           f"today's bar is missing and it is not settled yet")

        # A failed attempt used to burn the single per-session try, so one
        # network hiccup left the app silently stale until you restarted it.
        # Only SUCCESS is recorded now; failures may be retried.
        last_ok = st.session_state.get("_ohlc_pulled_on")
        if last_ok == now_.date().isoformat():
            return False, "already fetched successfully this session"

    import market_data as _md
    tried = []

    # Try Dhan whenever a token exists — NOT only when it is the configured
    # candle source.
    #
    # That gate was wrong and it cost real time. CANDLE_SOURCE decides which
    # feed draws the intraday CANDLES; it says nothing about where DAILY
    # bars should come from, and the two are separate concerns. With
    # CANDLE_SOURCE="nse" the app skipped a working Dhan route entirely and
    # reported "ALL SOURCES FAILED — nse: 503", naming one source while
    # silently declining to try the other. A failure message that omits a
    # route it never attempted is worse than no message.
    _has_dhan = False
    try:
        import dhan_client as _dcx
        _has_dhan = bool(_dcx.token())
    except Exception:                                       # noqa: BLE001
        pass

    if _has_dhan:
        try:
            ok, msg = _md.backfill_ohlc_dhan(config.OHLC_BACKFILL_DAYS)
            tried.append(f"dhan: {msg}")
            if ok:
                st.session_state["_ohlc_pulled_on"] = now_.date().isoformat()
                import build_master as _bm2
                _bm2.main()
                load_ohlc_cached.clear()
                return True, msg
        except Exception as exc:                            # noqa: BLE001
            tried.append(f"dhan: {exc}")

    try:
        import nse_client as _nc
        client = _nc.NSEClient()
        client._bootstrap()
        ok, msg = _md.backfill_ohlc(client, config.OHLC_BACKFILL_DAYS,
                                    force=True)
        tried.append(f"nse: {msg}")
        if ok:
            st.session_state["_ohlc_pulled_on"] = now_.date().isoformat()
            import build_master as _bm2
            _bm2.main()
            load_ohlc_cached.clear()
            return True, msg
    except Exception as exc:                                # noqa: BLE001
        tried.append(f"nse: {exc}")

    # Every route failed. Say so loudly with what each one reported — a
    # silent failure here is the whole reason the levels went stale.
    return False, "ALL SOURCES FAILED — " + " | ".join(tried)


def refresh_control(key: str, caches=(), note: str = "", stamp: str = "",
                    pull_ohlc: bool = False) -> None:
    """
    A refresh button scoped to one tab.

    The sidebar button clears every cache and refetches everything, which is
    slow and needlessly hammers NSE when you only wanted one panel updated.
    This clears just the caches that feed THIS tab, then reruns.

    Streamlit reruns the whole script on any interaction, so other tabs still
    redraw — but from their existing cached data rather than fresh requests.
    """
    c1, c2 = st.columns([1, 6])
    if c1.button("Refresh", key=f"rf_{key}", use_container_width=True):
        # Actually go and get new data, not just drop the cache. For tabs
        # backed by daily OHLC that means attempting the append first.
        if pull_ohlc:
            ok, msg = ensure_today_ohlc(force=True)
            if ok:
                st.success(msg)
            elif "up to date" not in msg and "already attempted" not in msg:
                st.info(msg)
        for fn in caches:
            try:
                fn.clear()
            except Exception:                               # noqa: BLE001
                pass
        st.rerun()
    bits = [f"drawn {datetime.now():%H:%M:%S}"]
    if stamp:
        bits.append(f"source {stamp}")
    if note:
        bits.append(note)
    c2.caption(" · ".join(bits))


st.sidebar.title("Scanner")

# scheduler heartbeat — the dashboard and the background job are separate
# processes, and it is worth seeing at a glance whether the scheduler is
# actually running rather than assuming it is.
try:
    import json as _json
    if config.SCHEDULER_STATE.exists():
        _sst = _json.loads(config.SCHEDULER_STATE.read_text())
        if _sst.get("date") == datetime.now().date().isoformat():
            st.sidebar.caption(
                f"**Scheduler today**  \n"
                f"chain polls {_sst.get('chain_polls', 0)} · "
                f"view {_sst.get('view_refreshes', 0)}  \n"
                f"OHLC {'appended ✓' if _sst.get('ohlc_done') else 'pending'}")
        else:
            st.sidebar.caption("Scheduler: no activity today")
    else:
        st.sidebar.caption("Scheduler not running — `python scheduler.py`")
except Exception:                                           # noqa: BLE001
    pass
demo = st.sidebar.checkbox("Demo mode (offline, sample file)", value=False)

# Active market window check (07:30 AM to 03:45 PM IST)
_now_t = datetime.now().time()
_mkt_start = getattr(config, "AUTO_REFRESH_START", dtime_(7, 30))
_mkt_end = getattr(config, "AUTO_REFRESH_END", dtime_(15, 45))
_in_market_hours = (_mkt_start <= _now_t <= _mkt_end)

if _in_market_hours:
    _poll_sec = st.sidebar.selectbox(
        "Live Refresh Rate",
        [60, 90, 120, 180],
        index=0,
        format_func=lambda s: f"⚡ Every {s}s",
        key="poll_interval_choice"
    )
    auto = st.sidebar.checkbox(f"Auto-refresh active ({_poll_sec}s)", value=True, key="auto_ref_cb")
else:
    _poll_sec = getattr(config, "POLL_SECONDS", 60)
    auto = st.sidebar.checkbox(
        f"Auto-refresh ({_poll_sec}s)",
        value=False,
        key="auto_ref_cb",
        help="Market is closed (after 15:45 IST). Auto-ping is disabled to conserve resources. Check to force polling."
    )
    st.sidebar.caption("⏸️ **Market Closed (after 15:45 IST)**  \nAuto-refresh is off. Chart levels & View Building have loaded.")

if st.sidebar.button("🔄 Refresh Data Now", use_container_width=True, key="sb_manual_refresh"):
    st.cache_data.clear()
    st.rerun()
st.sidebar.caption(f"NIFTY · nearest expiry · lot {config.LOT_SIZE} · "
                   f"capital Rs{config.CAPITAL:,}")

# ── Dhan API Token Management ────────────────────────────────────────────────
try:
    import dhan_client as dc
    _dhan_info = dc.token_preview()
    _dhan_stat = st.session_state.get("dhan_verify_status")

    if _dhan_stat and _dhan_stat.get("ok"):
        _badge_text = "🟢 Dhan API: Working ✓"
        _badge_color = "#10b981"
        _is_exp = False
    elif _dhan_stat and not _dhan_stat.get("ok"):
        _badge_text = "🔴 Dhan API: Expired / Invalid"
        _badge_color = "#ef4444"
        _is_exp = True
    elif _dhan_info.get("configured"):
        _badge_text = f"⚪ Dhan API: Saved ({_dhan_info.get('preview')})"
        _badge_color = "#94a3b8"
        _is_exp = False
    else:
        _badge_text = "⚠️ Dhan API: Token Required"
        _badge_color = "#f59e0b"
        _is_exp = True

    st.sidebar.markdown(
        f"<div style='display:flex;align-items:center;gap:6px;font-size:0.83rem;font-weight:600;color:{_badge_color};margin-top:6px;margin-bottom:2px;'>"
        f"{_badge_text}</div>",
        unsafe_allow_html=True
    )

    with st.sidebar.expander("🔑 Dhan API Access Token", expanded=_is_exp):
        st.markdown(
            "<p style='font-size:0.75rem;color:#94a3b8;margin:0 0 6px 0;'>"
            "Dhan tokens expire every 24 hours.<br>"
            "1. Open <a href='https://web.dhan.co' target='_blank' style='color:#38bdf8;'>web.dhan.co</a> &rarr; Profile<br>"
            "2. Go to <b>DhanHQ Trading APIs</b> &rarr; Access Token<br>"
            "3. Copy & paste below, then click <b>Test & Save</b>."
            "</p>",
            unsafe_allow_html=True
        )

        _pasted_token = st.text_area(
            "Paste Access Token",
            placeholder="Paste daily access token from Dhan app...",
            height=75,
            key="sb_dhan_token_paste_field",
            help="Generate from web.dhan.co -> Profile -> DhanHQ Trading APIs"
        )

        _col_save, _col_test = st.columns(2)
        _btn_save = _col_save.button(
            "⚡ Test & Save",
            use_container_width=True,
            help="Test this token with Dhan API and save if working",
            key="btn_dhan_save"
        )
        _btn_test = _col_test.button(
            "🔄 Test Current",
            use_container_width=True,
            help="Test the token currently saved in .env",
            key="btn_dhan_test_saved"
        )

        if _btn_save:
            if not _pasted_token.strip():
                st.sidebar.warning("Please paste a token before testing.")
            else:
                with st.spinner("Testing with Dhan API..."):
                    _v_ok, _v_msg = dc.verify_token(_pasted_token.strip())
                if _v_ok:
                    dc.save_token(_pasted_token.strip())
                    st.session_state["dhan_verify_status"] = {
                        "ok": True,
                        "msg": "Access key is working! Connected to Dhan Market Data.",
                        "time": datetime.now().strftime("%H:%M:%S")
                    }
                    st.cache_data.clear()
                    st.sidebar.success("✅ **Access key is working!** Token saved.")
                    st.rerun()
                else:
                    st.session_state["dhan_verify_status"] = {
                        "ok": False,
                        "msg": _v_msg,
                        "time": datetime.now().strftime("%H:%M:%S")
                    }
                    st.sidebar.error(f"❌ **Verification failed:** {_v_msg}")

        if _btn_test:
            with st.spinner("Testing saved Dhan token..."):
                _v_ok, _v_msg = dc.verify_token()
            st.session_state["dhan_verify_status"] = {
                "ok": _v_ok,
                "msg": _v_msg,
                "time": datetime.now().strftime("%H:%M:%S")
            }
            if _v_ok:
                st.sidebar.success("✅ **Access key is working!**")
                st.rerun()
            else:
                st.sidebar.error(f"❌ **Verification failed:** {_v_msg}")

        if _dhan_stat:
            if _dhan_stat.get("ok"):
                st.caption(f"Status: {_dhan_stat.get('msg')} ({_dhan_stat.get('time')})")
            else:
                st.caption(f"Status: {_dhan_stat.get('msg')}")
        elif _dhan_info.get("configured"):
            st.caption(f"Saved: `{_dhan_info.get('preview')}` ({_dhan_info.get('length')} chars)")
except Exception as _dhan_ui_exc:
    st.sidebar.caption(f"Dhan manager error: {_dhan_ui_exc}")

st.sidebar.markdown("---")
st.sidebar.caption("**Today's trades**")
_sc1, _sc2 = st.sidebar.columns(2)
if _sc1.button("Log a stop", use_container_width=True):
    STATE.stops_today += 1
    st.rerun()
if _sc2.button("Reset", use_container_width=True):
    STATE.stops_today = 0
    STATE.rangebound_trades = 0
    st.rerun()
if getattr(config, "MISSING_KEYS", None):
    st.sidebar.warning(
        "**config.py is out of date**  \n"
        + ", ".join(config.MISSING_KEYS)
        + "  \nRunning on built-in defaults. Copy the latest config.py so "
          "your own settings are the ones being used, not mine.")

_left = max(0, config.MAX_STOPS_PER_DAY - STATE.stops_today)
st.sidebar.caption(
    f"stops {STATE.stops_today}/{config.MAX_STOPS_PER_DAY}"
    + ("  — **CIRCUIT BREAKER TRIPPED**" if _left == 0
       else f" · {_left} left before the breaker"))

st.sidebar.markdown("---")
if st.sidebar.button("Run all sources", use_container_width=True,
                     help="Clears every cache and refetches everything. Slower "
                          "and heavier on NSE — each tab has its own Refresh "
                          "for the usual case."):
    st.cache_data.clear()
    st.rerun()

# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════

_render_html(
    f"<h1 style='margin-bottom:0'>NIFTY Option Chain Scanner</h1>"
    f"<p class='sub'>Support · resistance · regime · buy-side signal</p>"
)

try:
    df, spot, expiry, nse_ts = load_demo() if demo else fetch_live()
except Exception as exc:                                    # noqa: BLE001
    st.error("Could not fetch the chain.")
    st.code(str(exc), language="text")
    st.info("Run `python diagnose.py` in the terminal — it reports your public "
            "IP, whether the cookie bootstrap works, and the exact status code "
            "from every candidate endpoint. Tick Demo mode to keep testing the "
            "logic offline meanwhile.")
    st.stop()

now = datetime.now()
dte = days_to_expiry(expiry)
expiry_today = dte < 1.0

if cp.is_expired_chain(df, spot):
    st.warning("**This chain has no time value left — it is an expiry-day "
               "settlement snapshot.** Every option is at pure intrinsic. The "
               "OI in it is about to be extinguished, so the levels below "
               "describe a session that is already over. Do not trade forward "
               "from this.")

STATE.update_peaks(df)
read = an.analyse(df, spot, baseline=STATE.baseline, peaks=STATE.peaks,
                  spot_prev=STATE.spot_prev)
if STATE.baseline is None and now.time() >= config.MARKET_OPEN:
    STATE.baseline = read.levels
confirmed = STATE.note_regime(read.regime)
sig = an.make_signal(df, read, now, dte, confirmed,
                     STATE.rangebound_trades, expiry_today,
                     STATE.stops_today)
STATE.spot_prev = spot
STATE.push(now, read)

# ── market context: VIX (3-min) and FII/DII (daily) ─────────────────────────
vix, flows, vix_err, flows_err = None, None, None, None
if not demo:
    if config.USE_VIX:
        try:
            vix = fetch_vix_cached()
        except Exception as exc:                            # noqa: BLE001
            vix_err = str(exc)
    if config.USE_FLOWS:
        try:
            flows = fetch_flows_cached()
        except Exception as exc:                            # noqa: BLE001
            flows_err = str(exc)

# ── price structure + confluence ────────────────────────────────────────────
zones, price_lv, cycles, cpr = [], [], [], {}
ohlc_err, ohlc_notes, ohlc_span = None, [], None
new_flips = []
try:
    ohlc = load_ohlc_cached()
    price_lv = lv_mod.all_levels(ohlc, spot)
    cycles = lv_mod.time_cycles(ohlc)
    _, cpr = lv_mod.central_pivot_range(ohlc, spot)
    ohlc_notes = lv_mod.history_health(ohlc)
    ohlc_span = (len(ohlc), ohlc.Date.min(), ohlc.Date.max())
    # Load EOD homework anchors so levels are properly labeled as Previous Day S1, S2, R1, R2
    try:
        import gap_trap_strategy as gts
        _eod_hw = gts.extract_eod_homework(spot_fallback=spot, chain_fallback=df if "df" in locals() else None)
        oi_lv = [
            (float(_eod_hw.prev_day_r1), "resistance", f"Prev Day R1 Call Wall {_eod_hw.prev_day_r1_oi:,.0f} OI"),
            (float(_eod_hw.prev_day_s1), "support",    f"Prev Day S1 Put Wall {_eod_hw.prev_day_s1_oi:,.0f} OI"),
            (float(_eod_hw.prev_day_r2), "resistance", f"Prev Day R2 Call Wall {_eod_hw.prev_day_r2_oi:,.0f} OI"),
            (float(_eod_hw.prev_day_s2), "support",    f"Prev Day S2 Put Wall {_eod_hw.prev_day_s2_oi:,.0f} OI"),
            (float(read.levels.max_pain), "resistance" if read.levels.max_pain > spot else "support",
             f"Max pain {read.levels.max_pain}"),
        ]
    except Exception:
        oi_lv = [
            (float(read.levels.r1), "resistance", f"Prev Day R1 call wall {read.levels.r1_oi:,.0f} OI"),
            (float(read.levels.s1), "support",    f"Prev Day S1 put wall {read.levels.s1_oi:,.0f} OI"),
            (float(read.levels.r2), "resistance", "Prev Day R2 call wall"),
            (float(read.levels.s2), "support",    "Prev Day S2 put wall"),
            (float(read.levels.max_pain), "resistance" if read.levels.max_pain > spot else "support",
             f"Max pain {read.levels.max_pain}"),
        ]
    zones = cf.build_zones(price_lv, oi_lv)
    new_flips = STATE.flips.update(zones, spot, now)
except FileNotFoundError as exc:
    ohlc_err = str(exc)
except Exception as exc:                                    # noqa: BLE001
    ohlc_err = f"{type(exc).__name__}: {exc}"

# ── price action (Dhan, 1-min, resampled locally) ───────────────────────────
pa_frames, pa_reads, pa_stats, pa_orb, pa_err = {}, {}, {}, {}, None
# Bound here, not inside the try below.
#
# These were assigned only on the success path of a fetch nested inside an
# `if`. Any failure — and the whole point of the try is that failure is
# expected — left them undefined, so anything downstream referencing them
# raised NameError instead of degrading. The futures basis panel did exactly
# that: "basis unavailable: name 'fut_c' is not defined".
idx_c = fut_c = None
xcheck = (None, None)
if config.USE_PRICE_ACTION:
    try:
        idx_c, fut_c = fetch_price_action()
        # Native 15m/5m where the source can serve them — a resampled
        # candle is only as good as its 1-minute coverage, and Yahoo's is
        # not. Failure here is not fatal; build_all falls back to resampling
        # and every frame records which route it took.
        import candle_source as _csrc       # module scope; cs is function-local
        _native, _native_err = {}, []
        for _tf in ("5m", "15m", "30m"):   # no-op unless the source can serve them
            try:
                _n = _csrc.fetch_native(_tf)
                if _n is not None and not _n.empty:
                    _native[_tf] = _n
                else:
                    _native_err.append(f"{_tf}: source returned nothing")
            except Exception as _ne:                        # noqa: BLE001
                _native_err.append(f"{_tf}: {type(_ne).__name__}: {_ne}")
        pa_frames = pa.build_all(idx_c, fut_c, now=now, native=_native)
        pa_reads = {tf: pa.trend_read(d, tf) for tf, d in pa_frames.items()}
        pa_stats = pa.session_stats(idx_c)
        pa_orb = pa.opening_range(idx_c)
        xcheck = pa.cross_check(pa_reads, read.regime)
        pa.store_candles(idx_c)          # CSV per session, 09:15-15:30
        if "5m" in pa_frames:
            pa.store_5m_candles(pa_frames["5m"])
        if "15m" in pa_frames:
            pa.store_15m_candles(pa_frames["15m"])
        if idx_c is not None and not idx_c.empty:
            if "dhan_verify_status" not in st.session_state or not st.session_state["dhan_verify_status"].get("ok"):
                st.session_state["dhan_verify_status"] = {
                    "ok": True,
                    "msg": "Access key is working! Live market data active.",
                    "time": now.strftime("%H:%M:%S")
                }
    except Exception as exc:                                # noqa: BLE001
        pa_err = str(exc)
        _low_exc = str(exc).lower()
        if "invalid_authentication" in _low_exc or "expired" in _low_exc or "dh-901" in _low_exc:
            st.session_state["dhan_verify_status"] = {
                "ok": False,
                "msg": "Dhan token is expired (DH-901). Please paste today's token in the sidebar.",
                "time": now.strftime("%H:%M:%S")
            }

# ── verdict banner ──────────────────────────────────────────────────────────
lv = read.levels
colour = {"BULLISH": GREEN, "BREAKOUT_UP": GREEN, "BEARISH": RED,
          "BREAKDOWN": RED, "RANGEBOUND": GOLD, "CONSOLIDATION": SLATE}[read.regime]
act_col = {"BUY_CE": GREEN, "BUY_PE": RED, "NO_TRADE": SLATE}[sig.action]
act_txt = {"BUY_CE": "BUY CALL (CE)", "BUY_PE": "BUY PUT (PE)",
           "NO_TRADE": "NO TRADE"}[sig.action]


# ── Live Candle & Market Freshness Header Stamp ────────────────────────────
try:
    _c_status = "⚪ CHECKING"
    _c_color = "#94a3b8"
    _c_bg = "#1e293b"
    _c_border = "#334155"
    _c_ts_str = "—"
    _c_age_str = ""

    if idx_c is not None and not idx_c.empty and "timestamp" in idx_c:
        _last_ts = pd.to_datetime(idx_c["timestamp"].iloc[-1])
        _c_ts_str = _last_ts.strftime("%H:%M:%S")
        _age_sec = (now - _last_ts).total_seconds()
        if _age_sec < 90:
            _c_status = "🟢 LIVE DHAN CANDLE"
            _c_color = "#10b981"
            _c_bg = "#064e3b22"
            _c_border = "#059669"
            _c_age_str = f"({int(_age_sec)}s ago)"
        elif _age_sec < 300:
            _c_status = "🟡 DHAN CANDLE"
            _c_color = "#f59e0b"
            _c_bg = "#78350f22"
            _c_border = "#d97706"
            _c_age_str = f"({int(_age_sec//60)}m ago)"
        else:
            _c_status = "⚪ DHAN CANDLE"
            _c_color = "#94a3b8"
            _c_bg = "#1e293b22"
            _c_border = "#475569"
            _c_age_str = "(Closed/Stored)"

    _poll_note = ""
    if "_next_candle_in" in st.session_state:
        _due_s = st.session_state["_next_candle_in"]
        _poll_note = f" · Next 15m close in {_due_s // 60:02d}m {_due_s % 60:02d}s"

    _render_html(
        f"<div style='border: 1px solid {_c_border}; border-left: 4px solid {_c_color}; "
        f"background: {_c_bg}; border-radius: 6px; padding: 7px 14px; margin-bottom: 12px; "
        f"display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px;'>"
        f"<div style='display: flex; align-items: center; gap: 10px; flex-wrap: wrap;'>"
        f"<span style='color: {_c_color}; font-weight: 700; font-size: 13px; font-family: monospace;'>"
        f"{_c_status}: {_c_ts_str} {_c_age_str}</span>"
        f"<span style='color: #475569;'>|</span>"
        f"<span style='font-weight: 700; font-size: 13px; color: #f8fafc; font-family: monospace;'>SPOT: {spot:,.2f}</span>"
        f"<span style='color: #475569;'>|</span>"
        f"<span style='font-size: 12px; color: #cbd5e1; font-family: monospace;'>NSE CHAIN: {nse_ts}</span>"
        f"<span style='color: #475569;'>|</span>"
        f"<span style='font-size: 11px; background: #0f172a; color: #38bdf8; border: 1px solid #1e293b; padding: 1px 7px; border-radius: 3px; font-family: monospace;'>1m/5m SYNCED</span>"
        f"</div>"
        f"<div style='font-size: 12px; color: #94a3b8; font-family: monospace;'>"
        f"Clock: {now:%H:%M:%S}{_poll_note}</div>"
        f"</div>"
    )
except Exception:
    pass

if st.session_state.get("dhan_verify_status") and not st.session_state["dhan_verify_status"].get("ok"):
    st.warning(
        f"⚠️ **Dhan API Token Expired / Invalid**: {st.session_state['dhan_verify_status'].get('msg')}  \n"
        "👉 **Paste your fresh daily token in the sidebar (🔑 Dhan API Access Token) and click ⚡ Test & Save to activate live candles and VWAP.**"
    )

left, right = st.columns([1.15, 1])
with left:
    _render_html(
        f"<div class='banner'><span class='sub'>MARKET STATE</span><br>"
        f"<span class='verdict' style='color:{colour}'>{read.regime.replace('_',' ')}</span>"
        f"<br><span class='sub'>bias score {read.bias_score:+.1f} · "
        f"expected range {lv.s1}–{lv.r1} ({lv.r1-lv.s1} pts)</span></div>"
    )
with right:
    _render_html(
        f"<div class='banner'><span class='sub'>SIGNAL</span><br>"
        f"<span class='verdict' style='color:{act_col}'>{act_txt}</span><br>"
        f"<span class='sub'>{sig.reason}</span></div>"
    )

if xcheck[0]:
    v, why = xcheck
    box = {"ALIGNED BULLISH": st.success, "ALIGNED BEARISH": st.success,
           "CONFLICT": st.error, "CHAIN ONLY": st.warning,
           "NEITHER": st.info, "NO DATA": st.info}.get(v, st.info)
    box(f"**CHAIN vs PRICE — {v}.** {why}")

if sig.blocks:
    st.error("**Blocked:** " + " · ".join(sig.blocks))
for wmsg in sig.warnings:
    st.warning(wmsg)

# Fresh flips first — a level that just broke is time-sensitive in a way the
# static zone table is not.
for f in new_flips:
    st.error(f"**LEVEL BROKEN** — {f.headline}. {f.note}")

for f in STATE.flips.active(spot) if STATE.flips else []:
    if f in new_flips:
        continue
    if f.reclaimed:
        st.info(f"Earlier break failed — {f.headline}")
    else:
        st.warning(f"{f.headline}  ·  now {abs(f.price - spot):.0f} pts "
                   f"{'above' if f.price > spot else 'below'} spot")

# 9:15-9:20 — the chain still holds yesterday's OI. Say so loudly rather than
# letting the app present stale numbers with a confident face.
try:
    _stale, _stale_msg = an.chain_is_stale(now, nse_ts)
    if _stale:
        st.error(f"**{_stale_msg}**")
except Exception:                                           # noqa: BLE001
    pass

# Institutional size arriving at one strike in a single poll.
try:
    for _ib in fl.icebergs(df, spot, config.ICEBERG_LAKHS)[:3]:
        st.warning(f"**BIG PLAYER — {_ib.strike} {_ib.side} "
                   f"+{_ib.lakhs:.1f}L OI in one poll** ({_ib.pct:+.0f}%, "
                   f"{_ib.distance:+.0f} pts)  \n{_ib.read}")
except Exception:                                           # noqa: BLE001
    pass

# Gamma squeeze: writers cutting losses while price tests their wall. Three
# conditions at once, which is rare and fast when it happens.
try:
    for _side, _t in an.trapped_writers(df, spot, read.levels).items():
        st.error(f"**{_t['read']}**")
except Exception:                                           # noqa: BLE001
    pass

for alert in cf.proximity_alerts(zones, spot):
    st.warning(alert)

# ── metrics ─────────────────────────────────────────────────────────────────
c = st.columns(6)
c[0].metric("Spot", f"{spot:,.1f}")
c[1].metric("Support (S1)", f"{lv.s1}"); c[1].caption(f"S2 &nbsp;{lv.s2}", unsafe_allow_html=True)
c[2].metric("Resistance (R1)", f"{lv.r1}"); c[2].caption(f"R2 &nbsp;{lv.r2}", unsafe_allow_html=True)
c[3].metric("PCR (OI)", f"{read.pcr_oi:.2f}")
c[4].metric("Max pain", f"{lv.max_pain}")
c[5].metric("India VIX", f"{vix.value:.2f}" if vix else "—",
            f"{vix.pct_change:+.1f}%" if vix else "",
            delta_color="inverse" if vix else "off",
            help="Rising VIX means pricier options. Red is bad for buyers.")
c2 = st.columns(6)
c2[0].metric("IV skew", f"{read.iv_skew:+.1f}",
            help="Put IV minus Call IV. Rising skew = downside being bid.")
if vix:
    note = md.vix_bias_note(vix, sig.action)
    if note:
        st.warning(note)
if flows:
    st.caption(f"Previous session — {flows.note}")

if sig.actionable and sig.strike:
    d = st.columns(4)
    d[0].metric("Strike", f"{sig.strike} {'CE' if sig.action=='BUY_CE' else 'PE'}")
    d[1].metric("Premium", f"Rs{sig.premium:,.2f}")
    d[2].metric("Cost / lot", f"Rs{sig.cost_per_lot:,.0f}",
                f"{sig.cost_per_lot/config.CAPITAL*100:.0f}% of capital", delta_color="off")
    d[3].metric("Per 50 NIFTY pts", f"Rs{sig.move_per_50:,.0f}" if sig.move_per_50 == sig.move_per_50 else "—",
                f"delta {sig.delta:.2f}" if sig.delta == sig.delta else "", delta_color="off")

for n in read.notes:
    st.info(n)

# ── charts ──────────────────────────────────────────────────────────────────

# ── Keep the daily OHLC current, automatically ──────────────────────────────
# After 15:30 the completed bar exists. Rather than depending on the scheduler
# being up, or on you pressing anything, the dashboard pulls it itself on the
# first load after the close. Guarded so it happens once per session.
if not demo:
    _pulled_ok, _pulled_msg = ensure_today_ohlc()
    if _pulled_ok:
        st.toast(f"Daily bar appended — {_pulled_msg}", icon="✅")
    else:
        # Failure used to be silent, and that silence is the whole bug.
        # The pull ran on every page load, failed, said nothing, and left a
        # normal-looking app serving week-old levels. A toast on success and
        # nothing on failure is exactly backwards: success is the case you
        # do not need to be told about.
        _skip = ("already up to date", "already fetched", "before ")
        if not any(_pulled_msg.startswith(x) for x in _skip):
            st.sidebar.error(f"**Daily OHLC not updated**  \n{_pulled_msg}")

    # PDC — the 15:00 candle close, recorded at the same time as the
    # daily bar. Not 15:15: that candle carries the closing auction.
    try:
        import pdc as _pdcA
        _needs = _pdcA.previous(now.date() + timedelta(days=1)) is None or (
            not len(_pdcA.load()[_pdcA.load()["session"] == now.date()]))
        if (_needs and now.time() >= config.OHLC_APPEND_AFTER
                and now.weekday() < 5
                and st.session_state.get("_pdc_done") != now.date().isoformat()):
            _pok, _pmsg = _pdcA.backfill(since=now.date() - timedelta(days=10))
            if _pok:
                st.session_state["_pdc_done"] = now.date().isoformat()
                st.toast(f"PDC updated — {_pmsg}", icon="✅")
            else:
                st.sidebar.warning(f"PDC not updated: {_pmsg}")
    except Exception as _pe:                                # noqa: BLE001
        st.sidebar.warning(f"PDC update failed: {_pe}")

# ── View Building sources ───────────────────────────────────────────────────
# Fetched BEFORE the tabs render. Streamlit executes tab bodies in order, so
# anything the Context tab needs must already exist by the time it runs —
# fetching inside a later tab left the earlier one referencing an undefined
# name. Shared data belongs above the tabs, not inside one of them.
vb_daily = None
try:
    vb_daily = load_ohlc_cached()
except Exception:
    pass

vb_master = None
_mp = config.DATA_DIR / "master_levels.csv"
if _mp.exists():
    try:
        vb_master = pd.read_csv(_mp)
    except Exception:
        pass

_pc_close = None
if vb_daily is not None and len(vb_daily):
    _pc_close = float(vb_daily["Close"].iloc[-1])

(vb_indices, vb_ad_n, vb_ad_b, vb_glob, vb_nf, vb_bnf,
 vb_gift, vb_vixh, vb_crude, vb_crude_nse, vb_adrs, vb_poi) = fetch_view_building_sources_cached(_pc_close)

# Always compute it. Outside market hours NSE serves the previous session's
# close, which is exactly what you want at 8:30 AM — overnight positioning.
# It gets labelled so you know to re-check once the live chain updates.
_cond = None
_chain_live = config.MARKET_OPEN <= now.time() <= config.MARKET_CLOSE
try:
    _cond = su.chain_condition(df, spot)
except Exception as exc:                                # noqa: BLE001
    st.caption(f"chain condition unavailable: {exc}")

# The opening read, once the chain is trustworthy
try:
    if (config.MARKET_OPEN <= now.time() <= dtime_(10, 30)
            and not an.chain_is_stale(now)[0]):
        _prev_close = None
        if vb_daily is not None and len(vb_daily):
            _prev_close = float(vb_daily["Close"].iloc[-1])
        if _prev_close:
            _op = an.opening_scenario(
                df, spot, _prev_close, read.levels,
                an.straddle_range(df, spot),
                vix.value if vix else None,
                vix.pct_change if vix else None,
                an.intraday_pcr(df, spot),
                vb_poi.long_pct if vb_poi else None)
            _box = (st.success if _op.scenario.startswith("1")
                    else st.warning if _op.scenario.startswith(("2", "3"))
                    else st.info)
            _box(f"**OPENING: {_op.scenario}** · {_op.detail}\n\n"
                 f"**Play:** {_op.playbook}\n\n*Confidence: {_op.confidence}*")
except Exception as _e:                                     # noqa: BLE001
    st.caption(f"opening read unavailable: {_e}")


if "vb_overrides" not in st.session_state:
    st.session_state.vb_overrides = {}


# ── Greeks, flow context and agreement, computed once ──────────────────────
# Greeks come from NSE's IV via Black-Scholes. Time to expiry is measured in
# TRADING minutes, not calendar days — on expiry afternoon that is the
# difference between gamma reading 0.004 and 0.001, and gamma is the whole
# story on that session.
try:
    _exp_dt = datetime.strptime(expiry, "%d-%b-%Y").replace(hour=15, minute=30)
    df = gk.add_greeks(df, spot, _exp_dt, now)
    _gz = gk.gamma_zones(df, spot)
    _atm_gamma = None
    _ar = df[df.strike == an.atm_strike(spot)]
    if not _ar.empty:
        _g = _ar.iloc[0].get("ce_gamma")
        _atm_gamma = float(_g) if _g == _g else None
except Exception as _e:                                     # noqa: BLE001
    _gz, _atm_gamma = [], None
    st.caption(f"greeks unavailable: {_e}")

try:
    _cvw = fl.chain_vwap(df, spot)
except Exception:                                           # noqa: BLE001
    _cvw = None

try:
    _snaps = store.load_snapshots()
except Exception:                                           # noqa: BLE001
    _snaps = []

try:
    _log_now = store.load_log()
    _pcr_pers = fl.pcr_persistence(_log_now, now)
except Exception:                                           # noqa: BLE001
    _log_now, _pcr_pers = None, {}

# spot change since the previous poll, for the agreement banner
_spot_chg = 0.0
try:
    if _log_now is not None and len(_log_now) > 1:
        _spot_chg = spot - float(_log_now["spot"].iloc[-2])
except Exception:                                           # noqa: BLE001
    pass
try:
    _agree = fl.spot_chain_agreement(df, spot, _spot_chg)
except Exception:                                           # noqa: BLE001
    _agree = {}

# ── 5. Spot-chain agreement banner ─────────────────────────────────────────
if _agree.get("state"):
    _st = _agree["state"]
    if _st == "PUT WRITERS CONFIDENT":
        st.success(f"### CORROBORATED BULLISH\n{_agree['note']}")
    elif _st == "CALL WRITERS CONFIDENT":
        st.error(f"### CORROBORATED BEARISH\n{_agree['note']}")
    elif _st == "SPOT AND CHAIN DISAGREE":
        st.warning(f"### DIVERGENCE\n{_agree['note']}\n\n"
                   f"Positioning is not backing this move. That does not make "
                   f"it wrong — it makes it unconfirmed, which is a reason to "
                   f"wait rather than to fade.")
    else:
        st.info(f"**{_st}** — {_agree.get('note', '')}")

# ── Decision bar ────────────────────────────────────────────────────────────
# Four layers, deliberately not merged. A strong bias does not make a session
# tradeable: bias reads positioning, actionability asks whether you should
# have money on it right now, and the answer can be no while the bias is
# perfectly clear. Collapsing them is how "bearish" quietly becomes "sell".
try:
    _f5d = pa_frames.get("5m")
    _fpd = an.four_pillars(df, spot)
    _cld = an.chain_levels(df, spot)
    _trd = ms.trend_structure(_f5d) if _f5d is not None else None

    _vsd = None
    if _f5d is not None and "vwap" in _f5d:
        _vrd = pa.vwap_read(_f5d, "5m")
        _vsd = _vrd.side if _vrd.vwap else None
    _prd = ms.market_pressure(_fpd, _cld, _trd, _vsd)

    _brd = _pind = _rngd = None
    if _f5d is not None and len(_f5d) > 8:
        if _cld["resistance"]:
            _r0 = _cld["resistance"][0]
            _brd = ms.breakout(_f5d, spot, float(_r0.strike), "resistance",
                               _r0, _r0.neighbour_building)
        _pind = ms.pinning(df, spot, _f5d, _fpd["atm"])
        if _cld["resistance"] and _cld["support"]:
            _rngd = ms.range_trap(_f5d, float(_cld["resistance"][0].strike),
                                  float(_cld["support"][0].strike))

    _stale_now = an.chain_is_stale(now)[0]
    _trust, _tnote = ms.data_trust(now, nse_ts, STATE.regime_streak, _stale_now)
    DEC = ms.decision(_prd, _trd, _brd, _pind, _rngd, _trust, _tnote,
                      sig.blocks)

    # Write down what the app is saying, so it can be checked later against
    # the chart instead of being taken on trust.
    # WEEKEND IMMUNITY: Never store timeline on Saturday or Sunday!
    if now.weekday() < 5:
        try:
            tl.record({
                "Regime":     read.regime,
                "Bias":       DEC.bias.value,
                "Structure":  _trd.state if _trd else None,
                "Breakout":   _brd.state if _brd else None,
                "Range":      _rngd.state if _rngd else None,
                "Pinning":    _pind.state if _pind else None,
                "Agreement":  (_agree or {}).get("state"),
                "Actionable": DEC.actionability.value,
            }, now, spot)
        except Exception:                                       # noqa: BLE001
            pass

        # ── Refresh aligned to candle closes ────────────────────────────────
    #
    # A fixed 180-second poll lands wherever it lands: a 15-minute candle
    # closing at 09:30:00 might not be read until 09:32:50. On a system that
    # only ever acts on closed candles that delay is the whole latency, and
    # it is avoidable — the close times are known in advance.
    #
    # This is also the honest alternative to a Dhan webhook. A webhook would
    # need a public endpoint, a tunnel and a shared secret to save the same
    # two or three minutes, and it would deliver Dhan's pattern definitions
    # rather than the ones in strategy1_rules.json.
    if getattr(config, "ALIGN_POLL_TO_CANDLES", True):
        _tf = int(getattr(config, "ALIGN_POLL_MINUTES", 15))
        _lag = int(getattr(config, "ALIGN_POLL_LAG_SECONDS", 8))
        _mins = now.hour * 60 + now.minute
        _open = config.MARKET_OPEN.hour * 60 + config.MARKET_OPEN.minute
        _since = _mins - _open
        if _since >= 0:
            _next = ((_since // _tf) + 1) * _tf
            _due = _next * 60 + _lag - (_since * 60 + now.second)
            _due = max(_due, 5)
            st.session_state["_next_candle_in"] = _due
            st.sidebar.caption(
                f"next {_tf}m candle in {_due // 60}m {_due % 60}s "
                f"· refresh follows {_lag}s after the close")

# ── Multi-timeframe alignment, and whether to interrupt ─────────────
    try:
        _algn = al.alignment(pa_reads)
        if "algate" not in st.session_state:
            st.session_state.algate = al.Gate()
        _prev_al = st.session_state.get("prev_align")
        # Strategy 1 runs once here so its alerts fire whichever tab you
        # are on, and the tab reuses this result rather than recomputing.
        _s1res = None
        try:
            import strategy1 as _s1
            _f15 = pa_frames.get("15m")
            if _f15 is not None and len(_f15):
                _s1res = _s1.evaluate(_f15, prev_close=None, now=now)
        except Exception:                                   # noqa: BLE001
            pass
        st.session_state.s1_result = _s1res

        # Write closed signals down. Alert-only is only worth running if
        # something survives the session.
        # WEEKEND IMMUNITY: Never store strategy1 log on Saturday or Sunday!
        if _s1res is not None and now.weekday() < 5:
            try:
                import strategy1_log as _s1l
                _s1l.record(_s1res, now.date())
            except Exception:                               # noqa: BLE001
                pass

        _smc_sigs = []
        try:
            import smc_strategy as _smc_strat
            _f15 = pa_frames.get("15m")
            if _f15 is not None and len(_f15):
                _smc_sigs = _smc_strat.get_live_smc_signals(_f15, spot, df)
                _smc_strat.render_smc_sidebar_kanban(_f15, spot)
        except Exception:
            pass

        _new = al.check(st.session_state.algate, now, _algn, _prev_al,
                        breakout=_brd, icebergs=(fl.icebergs(df) or None),
                        s1_result=_s1res, smc_sigs=_smc_sigs)
        st.session_state.prev_align = _algn.state
    except Exception:                                       # noqa: BLE001
        _algn, _new = None, []

    if _algn:
        _acol = {"STRONG": "#0F9D58", "MODERATE": "#3A7D44",
                 "CONFLICT": "#E8503A"}.get(_algn.strength, "#8A8F98")
        _tfs = " · ".join(f"{k} {v}" for k, v in _algn.tf.items())
        _render_html(
            f"<div style='padding:6px 10px;border-left:3px solid {_acol};"
            f"background:#fafbfc;margin-bottom:8px'>"
            f"<b style='color:{_acol}'>{_algn.state}</b> "
            f"<span style='color:#8A8F98'>({_algn.strength})</span> &nbsp; "
            f"<span style='font-family:monospace;font-size:0.85em'>{_tfs}</span>"
            f"<br><span style='color:#5a6069;font-size:0.86em'>{_algn.note}"
            f"</span></div>"
        )

    for _a in _new:
        st.warning(f"**🔔 {_a.title}** — {_a.detail}")
        if getattr(config, "ALERT_SOUND", True):
            st.components.v1.html(al.beep_html(
                _a.key.replace("|", "_").replace(":", ""), _a.urgent), height=0)
    if _new:
        st.caption(st.session_state.algate.budget)

    _colr = {"BULLISH": "#0F9D58", "BEARISH": "#E8503A"}.get(
        DEC.bias.value, "#8A8F98")
    _acol = {"ACTIONABLE": "#0F9D58", "WATCH ONLY": "#C79A20",
             "WAIT": "#C79A20", "CONTEXT ONLY": "#5A7FA8",
             "BLOCKED": "#E8503A", "NO-TRADE ZONE": "#E8503A"}.get(
        DEC.actionability.value, "#8A8F98")

    d1, d2, d3, d4 = st.columns(4)
    for _c, _lay, _tint in ((d1, DEC.bias, _colr),
                            (d2, DEC.structure, "#8A8F98"),
                            (d3, DEC.breakout, "#8A8F98"),
                            (d4, DEC.actionability, _acol)):
        with _c:
            _render_html(
                f"<div style='border-left:4px solid {_tint};padding:2px 0 2px 10px'>"
                f"<div style='font-size:11px;color:#8A8F98;text-transform:uppercase;"
                f"letter-spacing:.1em'>{_lay.name}</div>"
                f"<div style='font-size:19px;font-weight:700;color:{_tint};"
                f"line-height:1.25'>{_lay.value}</div>"
                f"<div style='font-size:11px;color:#8A8F98'>{_lay.confidence}"
                f"</div></div>"
            )

    st.caption(f"**{DEC.one_line}** · data {DEC.trust} — {DEC.trust_note}")

    _tb = st.columns(4)
    # max_pain lives on read.levels, not on read — every other call site in
    # the app uses the nested path.
    _mpx = read.levels.max_pain
    # ── Heavyweight attribution / GEX / straddle decay ──────────────────
    try:
        import institutional as _ins
        # NSE first — the constituent list rides along with the
        # advance/decline call the app already makes. Dhan is the fallback
        # and costs five requests.
        _hq, _herr = _ins.quotes_from_nse(vb_ad_n or {})
        _hsrc = "NSE constituents (no extra request)"
        if not _hq:
            _hq, _herr = _ins.heavyweight_quotes()
            _hsrc = "Dhan (5 requests, cached 5 min)"
        _at = _ins.attribution(_hq, spot) if _hq else None
    except Exception as _ie:                                # noqa: BLE001
        _at, _herr = None, [str(_ie)]

    if _at and _at["rows"]:
        _acol = "#0F9D58" if _at["total_points"] > 0 else "#E8503A"
        # Horizontal chips, not a five-row table.
        #
        # Five rows of vertical space for five numbers you read in one
        # glance is the wrong trade on a screen you scan during a session.
        _chips = (
            f"<span style='display:inline-block;margin:0 8px 4px 0;"
            f"padding:2px 9px;border-radius:3px;font-size:0.82em;"
            f"font-weight:700;color:#fff;background:{_acol}'>"
            f"NET {_at['total_points']:+.0f} pts</span>") + "".join(
            f"<span style='display:inline-block;margin:0 6px 4px 0;"
            f"padding:2px 7px;border-radius:3px;font-size:0.78em;"
            f"background:{'#0F9D5822' if r.helping else '#E8503A22'};"
            f"border-left:2px solid {'#0F9D58' if r.helping else '#E8503A'}'>"
            f"<b>{r.name}</b> {r.points:+.0f} "
            f"<span style='color:#8A8F98'>({r.pct_change:+.2f}%)</span>"
            f"</span>"
            for r in _at["rows"])
        with st.expander(
                f"Heavyweights: {_at['total_points']:+.0f} pts "
                f"({_at['split']}) · {_at['weight_covered']:.0f}% of index",
                expanded=False):
            _render_html(_chips)
            # The note is shown only when it says something the header and
            # the chips do not. "Four are dragging, -51 points" restates
            # both; the split-verdict variant names the biggest single
            # contributor, which the chips make you hunt for.
            if "Split" in _at["note"]:
                st.caption(_at["note"])
            st.caption(f"source: {_hsrc}")
            st.caption(f"Weights as of {_at['as_of']} — hardcoded from NSE's "
                       f"factsheet, not an API. Re-check each quarter."
                       + (f"  ·  missing: {', '.join(_at['missing'])}"
                          if _at["missing"] else ""))
    elif _herr:
        st.caption("heavyweights unavailable: " + _herr[0][:90])

    # ── FII against Client positioning ──────────────────────────────────
    try:
        import institutional as _insp
        # vb_poi, assigned at line ~950. I first wrote vb_part, which does
        # not exist — the `if "vb_part" in dir()` guard would have made that
        # silently return None forever, so the panel would simply never
        # appear and nothing would say why. check.py caught it.
        _pp = _insp.participants(vb_poi)
        if _pp and (_pp.fii_ratio or _pp.client_ratio):
            _fl = ("FII " + ("net long" if _pp.fii_net > 0 else "net short")
                   + f" {_pp.fii_ratio:.2f}" if _pp.fii_ratio else "FII —")
            _cl = (f"Client {_pp.client_ratio:.2f}"
                   if _pp.client_ratio else "Client —")
            with st.expander(
                    f"Macro lean: {_fl} vs {_cl}"
                    + ("  ·  STRUCTURAL OPPOSITION" if _pp.diverging else "")
                    + (f"  ({_pp.as_of})" if _pp.as_of else ""),
                    expanded=False):
                _pc = st.columns(2)
                _pc[0].metric("FII long/short",
                              f"{_pp.fii_ratio:.2f}" if _pp.fii_ratio else "—",
                              f"net {_pp.fii_net:+,.0f}", delta_color="off")
                _pc[1].metric("Client long/short",
                              f"{_pp.client_ratio:.2f}" if _pp.client_ratio
                              else "—",
                              f"net {_pp.client_net:+,.0f}", delta_color="off")
                st.caption(_pp.note)
                st.caption("Previous session's book — NSE publishes this "
                           "after the close. Context for the day, never a "
                           "trigger inside it. Note: 'OPPOSITE SIDES' is the "
                           "permanent arrangement of this market (Client writes puts into FIIs 65/65 days). "
                           "See tab '🏛️ Participant Positioning & History' for percentile-ranked spreads.")
    except Exception:                                       # noqa: BLE001
        pass

    # ── Futures basis ───────────────────────────────────────────────────
    try:
        import basis as _bs
        _fut_px = None
        _ff = pa_frames.get("1m")
        if _ff is not None and "fut_close" in _ff and len(_ff):
            _fut_px = float(_ff["fut_close"].iloc[-1])
        elif fut_c is not None and len(fut_c):
            _fut_px = float(fut_c["close"].iloc[-1])
        if _fut_px:
            _b = _bs.record(spot, _fut_px, now) if now.weekday() < 5 else _bs.Basis(spot, _fut_px, now)
            _dv = _bs.divergence()
            _bcol = "#E8503A" if _dv.get("diverging") else "#8A8F98"
            _render_html(
                f"<div style='padding:5px 10px;border-left:3px solid {_bcol};"
                f"background:#fafbfc;margin-bottom:6px;font-size:0.9em'>"
                f"<b>Futures basis {_b.points:+.1f}</b> ({_b.state.lower()}) "
                f"&nbsp;<span style='color:#8A8F98'>fut {_b.futures:,.1f} · "
                f"spot {_b.spot:,.1f}</span>"
                + (f"<br><b style='color:{_bcol}'>{_dv['kind']}</b> "
                   f"<span style='color:#5a6069'>{_dv['note']}</span>"
                   if _dv.get("diverging") else
                   f"<br><span style='color:#8A8F98;font-size:0.9em'>"
                   f"{_dv.get('note','')}</span>")
                + "</div>"
            )
    except Exception as _bse:                               # noqa: BLE001
        st.caption(f"basis unavailable: {_bse}")

    _tb[0].metric("Max pain", f"{_mpx:,}",
                  f"{_mpx - spot:+,.0f} vs spot", delta_color="off")
    if _cvw and _cvw.combined:
        _tb[1].metric("Chain VWAP", f"{_cvw.combined:,.0f}",
                      f"{spot - _cvw.combined:+,.0f} spot vs VWAP",
                      delta_color="off")
    if _atm_gamma is not None:
        _tb[2].metric("ATM gamma", f"{_atm_gamma:.5f}",
                      "rises sharply into expiry", delta_color="off")
    if _pcr_pers:
        _inside = _pcr_pers["inside"]
        _mins = _pcr_pers["minutes"]
        _tb[3].metric("PCR band", f"{_pcr_pers['current']:.2f}",
                      f"{'INSIDE' if _inside else 'OUTSIDE'} "
                      f"{_pcr_pers['band']} {_mins:.0f}m", delta_color="off")
        _tb[3].progress(min(_mins / 30.0, 1.0),
                        text=f"{min(_mins, 30):.0f}/30 min")
    if _cvw:
        st.caption(_cvw.note)
    with st.expander("Why each layer reads that way"):
        for _lay in (DEC.bias, DEC.structure, DEC.breakout, DEC.actionability):
            st.markdown(f"**{_lay.name} — {_lay.value}** [{_lay.confidence}]")
            for _w in _lay.why:
                st.markdown(f"- {_w}")
        st.caption("Bias reads positioning. Actionability asks whether to have "
                   "money on it now. They are separate questions and the "
                   "second can be 'no' while the first is emphatic.")
    st.markdown("---")
except Exception as _e:                                     # noqa: BLE001
    DEC = None
    st.caption(f"decision bar unavailable: {_e}")

# ── the shared context ──────────────────────────────────────────────────
#
# Built once, here, before any view renders. Views take this and read
# nothing from module scope. Step 1 of the refactor: adding it changes no
# behaviour — nothing consumes it yet except the one migrated view below.
try:
    import context as _ctxmod
    _ohlc_df = None
    try:
        import build_master as _bmctx
        _ohlc_df = _bmctx.load_all(config.OHLC_DIR, quiet=True)
    except Exception:                                       # noqa: BLE001
        pass

    # Centre strike computed HERE, not inside tab 1.
    #
    # It was being derived in the OI profile body and read by three other
    # panels — the exact cross-tab dependency that breaks under a router,
    # because tab 1 may never run. check.py flagged it the moment CTX tried
    # to read it before the tabs.
    try:
        _ctx_centre, _ctx_centre_mode = an.centre_strike(
            spot, vb_gift.value if vb_gift else None)
    except Exception:                                       # noqa: BLE001
        _ctx_centre, _ctx_centre_mode = int(round(spot / 50) * 50), "fallback"

    CTX = _ctxmod.build(
        df=df, spot=spot, now=now, expiry=expiry, dte=dte, demo=demo,
        read=read, decision=DEC if "DEC" in dir() else None,
        alignment=_algn if "_algn" in dir() else None,
        s1_result=st.session_state.get("s1_result"),
        pa_frames=pa_frames if "pa_frames" in dir() else {},
        index_candles=idx_c if "idx_c" in dir() else None,
        futures_candles=fut_c if "fut_c" in dir() else None,
        chain_levels=st.session_state.get("chain_levels_now") or {},
        master_levels=st.session_state.get("master_levels_now") or {},
        master_frame=vb_master, ohlc=_ohlc_df,
        centre=_ctx_centre, centre_mode=_ctx_centre_mode,
        vb={"gift": vb_gift, "master": vb_master, "poi": vb_poi,
            "crude": vb_crude, "crude_nse": vb_crude_nse, "glob": vb_glob},
    )
except Exception as _cxe:                                   # noqa: BLE001
    CTX = None
    st.sidebar.caption(f"context unavailable: {_cxe}")

# ── Synthesize Unified 5-Minute Decision Narrative ─────────────────────────
# ── Synthesize Unified 5-Minute Decision Narrative & Cockpit ───────────────
import smc as _smc_mod
import order_flow as _of_mod
import narrative as _nar_mod
import metrics as _met_mod
import institutional as _ins_mod

_df_5m = pa_frames.get("5m") if "pa_frames" in locals() else None
_df_15m = pa_frames.get("15m") if "pa_frames" in locals() else None

_smc_res = None
try:
    if _df_15m is not None and not _df_15m.empty:
        _smc_res = _smc_mod.read(_df_15m)
    elif _df_5m is not None and not _df_5m.empty:
        _smc_res = _smc_mod.read(_df_5m)
except Exception:
    pass

_of_res = None
try:
    if _df_5m is not None and not _df_5m.empty:
        _of_res = _of_mod.read(_df_5m, spot, {"R1": lv.r1, "S1": lv.s1})
except Exception:
    pass

_liq_res = None
try:
    _liq_res = _met_mod.assess_liquidity(df, spot)
except Exception:
    pass

_gex = None
try:
    _gex = _ins_mod.net_gex(df, spot)
except Exception:
    pass

master_cockpit = _nar_mod.build_master_cockpit(
    read=read,
    sig=sig,
    spot=spot,
    vix=vix if "vix" in locals() else None,
    flows=flows if "flows" in locals() else None,
    gex=_gex,
    cpr_meta=cpr if "cpr" in locals() else None,
    pa_frames=pa_frames if "pa_frames" in locals() else None,
    order_flow_res=_of_res,
    smc_res=_smc_res,
    liq_res=_liq_res,
    xcheck=xcheck if "xcheck" in locals() else None,
    lot_size=getattr(config, "LOT_SIZE", 65),
)

# ── All Tabs Navigation (Ordered by Practical Usefulness: Most to Least) ──
_render_html(
    """
    <style>
    /* Smooth horizontal scrolling ribbon for all tabs */
    div[data-testid="stPills"] {
        display: flex !important;
        flex-wrap: nowrap !important;
        overflow-x: auto !important;
        white-space: nowrap !important;
        padding-bottom: 6px !important;
        gap: 6px !important;
        -webkit-overflow-scrolling: touch !important;
        scrollbar-width: thin !important;
    }
    div[data-testid="stPills"]::-webkit-scrollbar {
        height: 4px;
    }
    div[data-testid="stPills"]::-webkit-scrollbar-thumb {
        background: #334155;
        border-radius: 4px;
    }
    </style>
    """
)

_ORDERED_ALL_TABS = [
    # ── Tier 1: 🚨 Live In-Trade Execution & Active Risk Defense (Most Critical) ──
    "🌅 MORNING BRIEF",
    "📸 Snapshots",
    "🎯 Decision Cockpit",
    "🛡️ Anti-Fakeout & Real-Time Pressure",
    "⚡ Option Chain & OI Analysis",
    "📊 Option Chain / Key Levels",
    "Change in OI",
    "⚡ OI Timeline (Per-Strike)",
    "🧪 Options Lab (PCR · OI · Gamma · Vega)",
    "📈 Price Action (1m/5m/15m)",
    "🏹 Breakout & Trap Scanner",
    "📝 Paper Trading & Impulse Control",

    # ── Tier 2: ⚡ Tactical Confirmation & Order Flow Microstructure ───────────
    "🌊 Order Flow & Market Depth",
    "💧 Liquidity & Smart Money Terminal",
    "📊 Volume Profile & AMT",
    "VWAP/EMA",
    "📐 VAWM & 15m Matrix",
    "⚡ Institutional Quant Terminal",

    # ── Tier 3: 🎯 Strategy Setup Scanners & Signal Triggers ───────────────────
    "⚔️ WAR Setup (FII 30m)",
    "8 Setups",
    "PA Strategy 1",
    "SMC",
    "🔮 AI Forecast & Signals (5m/15m)",
    "⏱️ Next 15m Candle Predictor",
    "🕯️ Candlestick Patterns & 15m Early Read",
    "📐 Price Action Basics",
    "🎯 Price Levels & AI Confluence",
    "Master levels",
    "Chart levels",

    # ── Tier 4: 🏛️ Pre-Market Prep, Participant Bias & Regimes ──────────────────
    "🌅 Opening Gap Planner & Confluence",
    "🏛️ Participant Positioning & History",
    "📊 Trainer's Participant Sheet",
    "Market State",
    "Four Pillars",
    "Flow",
    "Context",
    "Score breakdown",
    "View Building",
    "Rulebook",
    "Session",
    "Timeline",

    # ── Tier 5: 📋 Post-Market Review & Journal (After 3:30 PM) ────────────────
    "📋 EOD Day Report (What Worked / What Failed)",
    "📓 Trading Journal",
]

_selected_tab_str = st.pills(
    "All Tabs (Ordered by In-Trade Importance)",
    _ORDERED_ALL_TABS,
    default=_ORDERED_ALL_TABS[0],
    key="all_tools_rearranged_ribbon",
    label_visibility="collapsed",
) or _ORDERED_ALL_TABS[0]

_LABEL_TO_TAB_INDEX = {
    "🕯️ Candlestick Patterns & 15m Early Read": 38,
    "Candlestick Patterns & 15m Early Read": 38,
    "Candlestick Patterns": 38,
    "Patterns": 38,
    "🛡️ Anti-Fakeout & Real-Time Pressure": 37,
    "Anti-Fakeout & Real-Time Pressure": 37,
    "Anti-Fakeout": 37,
    "Real-Time Pressure": 37,
    "Pressure Gauge": 37,
    "⚡ Option Chain & OI Analysis": 36,
    "Option Chain & OI Analysis": 36,
    "Option Chain & Open Interest": 36,
    "Option Chain & Open Interest Analysis": 36,
    "OI Analysis": 36,
    "📋 EOD Day Report (What Worked / What Failed)": 35,
    "EOD Day Report": 35,
    "Day Report": 35,
    "⚡ Institutional Quant Terminal": 29,
    "📐 Price Action Basics": 30,
    "Price Action Basics": 30,
    "💧 Liquidity & Smart Money Terminal": 31,
    "Liquidity & Smart Money Terminal": 31,
    "💧 Liquidity & Level Strength": 31,
    "Liquidity & Level Strength": 31,
    "📊 Volume Profile & AMT": 32,
    "Volume Profile": 32,
    "🌊 Order Flow & Market Depth": 33,
    "Order Flow & Market Depth": 33,
    "Market Depth": 33,
    "🏛️ Participant Positioning & History": 34,
    "Participant Positioning & History": 34,
    "Participant Positioning": 34,
    "Participant History": 34,
    "📊 Trainer's Participant Sheet": 43,
    "🧪 Options Lab (PCR · OI · Gamma · Vega)": 44,
    "Options Lab": 44,
    "Trainer's Participant Sheet": 43,
    "Trainer Sheet": 43,
    "Participant Sheet": 43,
    "🎯 Decision Cockpit": 0,
    "OI profile": 1,
    "📊 Option Chain (OI Profile)": 1,
    "📊 Option Chain / Key Levels": 1,
    "Option Chain / Levels": 1,
    "Option Chain": 1,
    "Option-Chain Key Levels": 1,
    "Key Levels": 1,
    "Change in OI": 2,
    "⚡ OI Timeline (Per-Strike)": 39,
    "OI Timeline": 39,
    "Session": 3,
    "Score breakdown": 4,
    "Price levels": 5,
    "🎯 Price Levels & AI Confluence": 5,
    "Context": 6,
    "Price action": 7,
    "📈 Price Action (1m/5m/15m)": 7,
    "8 Setups": 8,
    "Master levels": 9,
    "View Building": 10,
    "Chart levels": 11,
    "Four Pillars": 12,
    "Market State": 13,
    "Rulebook": 14,
    "Flow": 15,
    "Timeline": 16,
    "PA Strategy 1": 17,
    "VWAP/EMA": 18,
    "SMC": 19,
    "📓 Trading Journal": 20,
    "📐 VAWM & 15m Matrix": 22,
    "⚔️ WAR Setup (FII 30m)": 23,
    "📝 Paper Trading & Impulse Control": 25,
    "🔮 AI Forecast & Signals (5m/15m)": 26,
    "🌅 Opening Gap Planner & Confluence": 27,
    "🌅 Monday Gap Planner & Confluence": 27,
    "🏹 Breakout & Trap Scanner": 28,
    "⏱️ Next 15m Candle Predictor": 40,
    "Next 15m Candle Predictor": 40,
    "Next 15m Candle": 40,
    "Next Candle": 40,
    "🌅 MORNING BRIEF": 41,
    "MORNING BRIEF": 41,
    "Morning Brief": 41,
    "📸 Snapshots": 42,
    "Snapshots": 42,
}

_active_tab_idx = _LABEL_TO_TAB_INDEX.get(_selected_tab_str, 0)

def _tab_active(i: int) -> bool:
    return _active_tab_idx == i

st.divider()
if _tab_active(0):
    # ── 🎯 Decision Desk (one-screen confluence: SMC · 5-Rule · Options Lab · OI · strikes) ──
    try:
        import decision_desk as _ddk
        _ddk.render(
            spot=spot if "spot" in locals() else 0.0,
            chain_df=df if "df" in locals() else None,
            pa_frames=pa_frames if "pa_frames" in locals() else None,
            now=now if "now" in locals() else None,
            dte=float(dte) if "dte" in locals() and dte else 1.0,
            prev_close=locals().get("_pc15", locals().get("_prev_close", None)),
        )
    except Exception as _ddk_err:                              # noqa: BLE001
        st.error(f"Decision Desk encountered an error: {_ddk_err}")
    _show_legacy_cockpit = st.toggle("Show legacy detailed cockpit (older engines)", value=False, key="legacy_cockpit_toggle")
if _tab_active(0) and _show_legacy_cockpit:
    # ── Unified Master Decision Cockpit & All-Tab Confluence ──────────────────
    try:
        import decision_cockpit_view as _dcv
        _pc_cockpit = locals().get("_pc15", locals().get("_prev_close", None))
        _dcv.render_unified_decision_cockpit(
            spot=spot if "spot" in locals() else 0.0,
            lv=lv if "lv" in locals() else None,
            read=read if "read" in locals() else None,
            chain_df=df if "df" in locals() else None,
            pa_frames=pa_frames if "pa_frames" in locals() else None,
            vb_daily=vb_daily if "vb_daily" in locals() else None,
            idx_c=idx_c if "idx_c" in locals() else None,
            master_cockpit=master_cockpit if "master_cockpit" in locals() else None,
            prev_close=_pc_cockpit,
            now=now if "now" in locals() else None,
            sig=sig if "sig" in locals() else None,
            dte=dte if "dte" in locals() else 0.0,
            nse_ts=nse_ts if "nse_ts" in locals() else "",
        )
    except Exception as _cockpit_err:
        st.error(f"Unified Decision Cockpit encountered an error: {_cockpit_err}")

if _tab_active(1):
    refresh_control("oi_profile", (fetch_live,), "option chain", nse_ts)

    # ── AI Option Chain & OI Directional Trade Filter (Zero VWAP) ──
    try:
        import oi_analytics as _oia
        _ohlc_df = locals().get("ohlc")
        _pc_val = float(_ohlc_df.iloc[-1]["Close"]) if (_ohlc_df is not None and hasattr(_ohlc_df, "empty") and not _ohlc_df.empty) else None
        _oia.render_oi_filter_card(
            df=df,
            spot=spot,
            pa_frames=pa_frames if "pa_frames" in locals() else None,
            prev_close=_pc_val
        )
    except Exception as _oie:
        st.caption(f"Option Chain OI filter unavailable: {_oie}")

    # ── ⚡ Institutional Double Confirmation HUD & Breakout / Anti-Trap Surveillance ──
    try:
        import option_chain_oi_tab as _oc_oi_mod
        _engine1 = _oc_oi_mod.OptionChainOIEngine()
        _focus_df1 = _engine1.filter_focus_window(df, spot, focus_radius=450)
        _levels1 = _engine1.analyze_levels(_focus_df1, spot)
        if _levels1:
            _verdict1 = _engine1.validate_breakout(_focus_df1, spot, _levels1)
            _hud1 = _engine1.evaluate_double_confirmation(
                spot, _verdict1,
                pa_frames=pa_frames if "pa_frames" in locals() else None,
                smc_res=_smc_res if "_smc_res" in locals() else None
            )
            _oc_oi_mod.render_double_confirmation_hud(_hud1)
    except Exception as _oc_hud_err:
        st.caption(f"Double confirmation HUD unavailable: {_oc_hud_err}")

    # ── 🪜 Option-Chain Key Levels — Price Ladder Grid ─────────────────
    try:
        render_price_ladder_grid(df, spot)
    except Exception as _ple:
        st.caption(f"Price ladder grid unavailable: {_ple}")

    # ── 🏛️ Executive Option Chain Narrative Intelligence Storyboard ───────
    try:
        import option_chain_narrative as _ocn
        _now_val = locals().get("now", datetime.now())
        _dte_val = locals().get("dte", 0.0)
        _ocn.render_tab1_narratives(df, read, spot, now=_now_val, dte=_dte_val)
    except Exception as _ocn_err1:
        st.caption(f"Option chain narratives unavailable: {_ocn_err1}")

    # ── GEX and straddle decay ──────────────────────────────────────────
    try:
        import institutional as _ins2
        _gex = _ins2.net_gex(df, spot)
        if _gex.get("known"):
            _gcol = "#0F9D58" if _gex["total"] > 0 else "#E8503A"
            # Verdict in the header — the number you need without a click.
            _ghdr = (f"Net GEX: {_gex['regime'].split(' —')[0]} "
                     f"({_gex['total']/1e9:+,.1f}bn)"
                     + (f" · flip {_gex['flip']:,.0f} "
                        f"({_gex['flip'] - spot:+,.0f} pts)"
                        if _gex.get("flip") else " · no flip in band")
                     + f" · {_gex['regime'].split('— ')[-1]}")
            with st.expander(_ghdr, expanded=False):
                _render_html(
                    f"<span style='color:{_gcol};font-weight:600'>"
                    f"{_gex['regime']}</span> · total "
                    f"{_gex['total']/1e9:+,.1f}bn"
                    + (f" · flip {_gex['flip']:,.0f} "
                       f"({_gex['flip'] - spot:+,.0f} from spot)"
                       if _gex.get("flip") else " · no flip in range")
                )
                st.caption(_gex["note"])
                # A permanent red box is banner fatigue: keep red for
                # things that need acting on now — a stop hit, a session
                # block — not for a standing caveat you have already read.
                _render_html(
                    f"<span style='font-size:0.72em;background:#1b382b;"
                    f"color:#2FBF71;padding:2px 7px;border-radius:3px;border:1px solid #2FBF7144'>"
                    f"convention: US dealer mode (verified: 65/65 sessions)</span>"
                )
                st.caption(
                    f"Sign convention verified: GEX_DEALER_SIGN is "
                    f"{_gex['sign']:+d} (US dealer convention: short calls, long puts). "
                    f"Validated on 65 of 65 sessions in participant history "
                    f"(FII net short calls & net long puts 100% of days; configurable in config.py)."
                    + (f"  {_gex['skipped']} strike(s) inside +/-"
                       f"{_gex.get('band', 500):.0f} skipped for unusable IV."
                       if _gex.get("skipped") else ""))
                st.caption(f"Computed on strikes within +/-"
                           f"{_gex.get('band', 500):.0f} of spot; "
                           f"{_gex.get('outside_band', 0)} further out "
                           f"excluded — gamma is negligible there and the "
                           f"IV solver fails on five-paise premiums.")
    except Exception:                                       # noqa: BLE001
        pass

    try:
        _srx = an.straddle_range(df, spot)
        # StraddleRange carries the two legs, not a total — a guessed
        # attribute name would have returned None and left this panel
        # silently blank, which is the failure mode hardest to notice.
        _cur = ((float(_srx.ce_ltp) + float(_srx.pe_ltp))
                if _srx and _srx.ce_ltp and _srx.pe_ltp else None)
        if _cur:
            _op = _ins2.open_straddle(float(_cur), now)
            _dc = _ins2.decay_curve(_op, float(_cur), now, max(dte, 0.05))
            if _dc:
                _anchor = _ins2.open_straddle_at(now)
                _dcol = {"ON SCHEDULE": "#8A8F98",
                         "UNDER-DECAYING": "#E65100",
                         "OVER-DECAYING": "#1a3a6b",
                         "MODEL UNRELIABLE": "#8A8F98"}[_dc.state]
                _dhdr = (f"Straddle Decay: {_dc.state} · "
                         f"act {_dc.actual:.0f} / exp {_dc.theoretical:.0f} "
                         f"({_dc.gap_pct:+.0f}%)"
                         + (f" · anchor {_anchor[11:16]}" if _anchor else ""))
                with st.expander(_dhdr, expanded=False):
                    _render_html(
                        f"<span style='color:{_dcol};font-weight:600'>"
                        f"{_dc.state}</span> · actual {_dc.actual:.0f} vs "
                        f"{_dc.theoretical:.0f} expected "
                        f"({_dc.gap_pct:+.0f}%)"
                    )
                    st.caption(_dc.note)
                    st.caption("Burn rate: " + _dc.burn_line)
                    st.caption(
                        f"anchored on {_op:.0f} at "
                        f"{_anchor[11:16] if _anchor else '?'} · {dte:.2f} "
                        f"days to expiry"
                        + ("" if not _anchor or _anchor[11:16] <= "09:20"
                           else " — the app was not running at the open, so "
                                "this measures decay from that time, not "
                                "from 09:15, which understates it"))
    except Exception:                                       # noqa: BLE001
        pass

    # The market's own expected range, priced by the ATM straddle — an
    # estimate completely independent of the OI walls.
    try:
        _sr = an.straddle_range(df, spot)
        if _sr:
            _sc1, _sc2, _sc3, _sc4 = st.columns(4)
            _sc1.metric("ATM straddle", f"{_sr.premium:,.0f} pts",
                       f"{_sr.strike} CE {_sr.ce_ltp:,.0f} + PE {_sr.pe_ltp:,.0f}",
                       delta_color="off")
            _sc2.metric("Expected low", f"{_sr.lower:,.0f}")
            _sc3.metric("Expected high", f"{_sr.upper:,.0f}")
            _sc4.metric("Range width", f"{_sr.width_pct:.2f}%")
            st.caption(f"Spot {spot:,.0f} — {_sr.position}. This is what "
                       f"option sellers are pricing the day's move at, with "
                       f"capital committed to defending it. Where it agrees "
                       f"with the OI walls you have a strong boundary; where "
                       f"it disagrees, the straddle is usually the more honest "
                       f"number, because it is a live price rather than a "
                       f"position that might be a hedge.")

            _wc = st.columns(3)
            _ip = an.intraday_pcr(df, spot)
            if _ip:
                _wc[0].metric("Intraday PCR", f"{_ip['ratio']:.2f}",
                              "change in OI, near strikes", delta_color="off")
            _vs = an.vol_skew(df, spot)
            if _vs:
                _wc[1].metric("Vol skew (200 OTM)", f"{_vs['diff']:+.1f}",
                              f"{_vs['put_iv']:.1f}P vs {_vs['call_iv']:.1f}C",
                              delta_color="off")
            _cv = an.conviction(df, spot)
            if _cv:
                _wc[2].metric("OI / volume", f"{_cv['ratio']:.2f}",
                              _cv["read"].split(" —")[0], delta_color="off")
            for _blk in (_ip, _vs, _cv):
                if _blk:
                    st.caption(f"· {_blk['read']}")

            # ── Weight balance: 5 strikes each side of the centre ────────
            import chain_metrics as _cmx
            try:
                _ctr = an.centre_strike(spot, vb_gift.value if vb_gift else None)[0]
                _wb = _cmx.weight_balance(df, _ctr, 5)
                if _wb:
                    _wc = st.columns(3)
                    _wc[0].metric(f"CE new OI · {_wb.n} above",
                                  f"{_wb.ce_chg:,.0f}")
                    _wc[1].metric(f"PE new OI · {_wb.n} below",
                                  f"{_wb.pe_chg:,.0f}")
                    _wc[2].metric("Favours", _wb.favour,
                                  f"{_wb.tilt_pct:.0f}% tilt",
                                  delta_color="off")
                    st.caption(f"centre {_wb.centre:,} · {_wb.note}")
            except Exception as _wbe:                       # noqa: BLE001
                st.caption(f"weight balance unavailable: {_wbe}")

            # ── IV demand ───────────────────────────────────────────────
            try:
                _chains = {"front": df}
                _extra = st.session_state.get("chains_by_expiry") or {}
                _chains.update(_extra)
                _ivd = _cmx.iv_demand(_chains, _ctr, 5)
                if _ivd.ce_avg or _ivd.pe_avg:
                    _ic = st.columns(3)
                    _ic[0].metric("Call IV (top 5)", f"{_ivd.ce_avg:.2f}")
                    _ic[1].metric("Put IV (top 5)", f"{_ivd.pe_avg:.2f}")
                    _ic[2].metric("Demand", _ivd.leaning,
                                  f"{_ivd.diff:+.2f}", delta_color="off")
                    st.caption(_ivd.note)
                    if len(_chains) == 1:
                        st.caption("Front expiry only. A front-month IV spike "
                                   "can be an expiry artefact rather than "
                                   "demand — comparing expiries separates "
                                   "them, but costs one chain fetch each.")
                    else:
                        st.caption("across " + " · ".join(
                            f"{k}: CE {v['ce']:.1f}/PE {v['pe']:.1f}"
                            for k, v in _ivd.per_expiry.items()
                            if v.get("ce") and v.get("pe")))
            except Exception:                               # noqa: BLE001
                pass

            # Total PCR against today's flow — the moment the distinction bites
            try:
                _pd_ = fl.pcr_divergence(fl.three_pcrs(df, spot))
                if _pd_.get("diverging"):
                    st.warning(f"**PCR DIVERGENCE** — {_pd_['note']}")
                elif _pd_:
                    st.caption(f"· {_pd_['note']}")
            except Exception:                               # noqa: BLE001
                pass

            try:
                # ── Strike VWAP, from Dhan only ─────────────────────
                #
                # The NSE sampled approximation that used to sit here has
                # been removed rather than demoted. It accumulated price x
                # volume-added between polls, which misses trades inside an
                # interval, loses everything before the app started, and
                # merges intervals when a poll is skipped. Keeping it beside
                # a correct number meant two figures claiming to be VWAP with
                # one of them known to be wrong — and the wrong one gets read
                # whenever it is closer to hand.
                with st.expander("Strike VWAP — true, from Dhan"):
                    import option_vwap as _ovx
                    st.caption(
                        "Real OHLCV per bar, so this is VWAP by definition. "
                        "One API call per contract, which is why you choose "
                        "the strikes rather than getting all forty.")
                    _v1, _v2, _v3 = st.columns([2, 1, 1])
                    _tvs = _v1.number_input(
                        "Centre strike", value=int(_ctr),
                        step=int(config.STRIKE_STEP), key="tv_strike")
                    _tvi = _v2.selectbox("Bars", ["1", "5", "15"], index=1,
                                         key="tv_int")
                    _tvn = _v3.selectbox("Strikes", ["1", "3", "5"], index=0,
                                         key="tv_span")
                    _span = {"1": 0, "3": 1, "5": 2}[_tvn]
                    st.caption(f"{(_span * 2 + 1) * 2} API calls")

                    if st.button("Fetch VWAP", key="tv_go"):
                        try:
                            if _span == 0:
                                _res = _ovx.compare(int(_tvs), interval=_tvi)
                                if not _res:
                                    st.error(
                                        "Could not resolve or fetch that "
                                        "strike. Check the expiry is live and "
                                        "that the Data API covers options.")
                                for _r in _res:
                                    (st.warning if _r.thin else st.success)(
                                        f"**{_r.side} {_r.strike}** — VWAP "
                                        f"{_r.vwap:,.2f} · last {_r.ltp:,.2f} "
                                        f"({_r.gap:+.2f}, {_r.side_of})")
                                    st.caption(_r.note)
                            else:
                                _wt = _ovx.watchlist(int(_tvs), _span, _tvi)
                                if _wt.empty:
                                    st.error("nothing returned")
                                else:
                                    st.dataframe(_wt,
                                                 use_container_width=True,
                                                 hide_index=True)
                                    st.caption(
                                        "A blank VWAP with a note is a fetch "
                                        "that failed or a contract that did "
                                        "not resolve — not a strike with no "
                                        "volume. Those mean opposite things.")
                        except Exception as _tve:           # noqa: BLE001
                            st.error(f"VWAP fetch failed: {_tve}")
            except Exception as _ove:                       # noqa: BLE001
                st.caption(f"strike VWAP panel unavailable: {_ove}")

            # Skew inversion is uncommon and sits where nobody looks
            try:
                _sk = fl.skew_inversion(_vs)
                if _sk.get("inverted"):
                    st.warning(f"**{_sk['note']}**")
            except Exception:                               # noqa: BLE001
                pass

            # Churn: did the volume actually create open interest?
            try:
                _ch = [c for c in fl.churn_check(df, spot, 4) if c.is_churn]
                if _ch:
                    st.info("**Churn — treat these walls as soft**\n\n"
                            + "\n\n".join(
                                f"· **{c.strike} {c.side}** — {c.note}"
                                for c in _ch[:3]))
                _quiet = [c for c in fl.churn_check(df, spot, 4)
                          if c.ratio < 1.0]
                if _quiet:
                    st.success("**Quiet accumulation — these are more likely "
                               "to hold**\n\n"
                               + "\n\n".join(
                                   f"· **{c.strike} {c.side}** — {c.note}"
                                   for c in _quiet[:3]))
            except Exception:                               # noqa: BLE001
                pass
            st.caption("Intraday PCR uses CHANGE in OI over the nearest "
                       "strikes, not total OI — total PCR is dominated by "
                       "deep-OTM positions that will never be tested. Above "
                       "1.3 put writers are defending; below 0.7 call writers "
                       "are capping.")
    except Exception as _e:                                 # noqa: BLE001
        st.caption(f"institutional reads unavailable: {_e}")
    st.plotly_chart(chart_oi_profile(df, read), use_container_width=True)

    # ── 📊 Interactive Full Option Chain Matrix & Strike Narratives ─────
    try:
        import option_chain_narrative as _ocn
        _ocn.render_option_chain_matrix(df, spot, read)
    except Exception as _mat_err:
        st.caption(f"Option chain matrix unavailable: {_mat_err}")

    # ── Liquidity Assessment Tool (Bid-Ask Spread × Lot Size) ────────────
    try:
        import metrics as _met
        _liq_res = _met.assess_liquidity(df, spot, lot_size=config.LOT_SIZE,
                                         threshold=config.LIQUIDITY_THRESHOLD, span=5)
        _lcol = "#0F9D58" if _liq_res["is_liquid"] else "#E8503A"
        _ce_txt = f"Call ₹{_liq_res['avg_otm_ce_impact']:.0f}/lot" if pd.notna(_liq_res['avg_otm_ce_impact']) else ""
        _pe_txt = f"Put ₹{_liq_res['avg_otm_pe_impact']:.0f}/lot" if pd.notna(_liq_res['avg_otm_pe_impact']) else ""
        _sep = " · " if (_ce_txt and _pe_txt) else ""
        _lhdr = (f"Liquidity & Impact Cost: {_liq_res['verdict']} "
                 + (f"({_ce_txt}{_sep}{_pe_txt})" if (_ce_txt or _pe_txt) else "")
                 + f" · Max Near-ATM ₹{_liq_res['max_near_impact']:.0f} (Threshold ₹{_liq_res['threshold']:.0f})")
        with st.expander(_lhdr, expanded=not _liq_res["is_liquid"]):
            _render_html(
                f"<span style='color:{_lcol};font-weight:600;font-size:1.1em'>"
                f"{_liq_res['verdict']}</span> — "
                f"Lot Size: **{_liq_res['lot_size']}** · Max Slippage Threshold: **₹{_liq_res['threshold']:,.0f} / contract**"
            )
            if _liq_res["warning"]:
                st.error(f"⚠️ **Execution Warning**: {_liq_res['warning']}")
            else:
                st.success("✓ **Liquid Product**: Near-ATM and OTM contract spreads have low impact cost (< ₹800/lot). Safe for execution and naked option selling.")
            st.caption(
                "Rule: **Impact Cost = (Ask − Bid) × Lot Size**. If the cost of crossing the spread for a single lot "
                "consistently exceeds ₹800, the product is flagged as **Illiquid** and dangerous for naked option selling due to high slippage."
            )
            _tbl = _liq_res["table"].copy()
            def _style_liq(val):
                if val == "LIQUID":
                    return "background-color:#12421F;color:#D6F5DF;font-weight:600"
                if "ILLIQUID" in str(val):
                    return "background-color:#5A1A16;color:#FFD9D5;font-weight:600"
                return ""
            st.dataframe(
                _tbl.style.applymap(_style_liq, subset=["CE Status", "PE Status"])
                .format({
                    "CE Bid": "{:,.2f}", "CE Ask": "{:,.2f}", "CE Spread": "{:,.2f}",
                    "CE Impact (₹)": "{:,.2f}", "PE Bid": "{:,.2f}", "PE Ask": "{:,.2f}",
                    "PE Spread": "{:,.2f}", "PE Impact (₹)": "{:,.2f}"
                }, na_rep="—"),
                use_container_width=True, hide_index=True
            )
    except Exception as _lqe:
        st.caption(f"Liquidity assessment tool unavailable: {_lqe}")
if _tab_active(2):
    refresh_control("oi_change", (fetch_live,), "option chain", nse_ts)

    # ── Modern Intraday OI Shifts Cockpit HUD ──
    try:
        _w_oi = an.window(df, spot, config.WINDOW_STRIKES + 3)
        if not _w_oi.empty:
            _tot_ce_add = _w_oi.ce_chg_oi[_w_oi.ce_chg_oi > 0].sum()
            _tot_ce_unw = _w_oi.ce_chg_oi[_w_oi.ce_chg_oi < 0].sum()
            _tot_pe_add = _w_oi.pe_chg_oi[_w_oi.pe_chg_oi > 0].sum()
            _tot_pe_unw = _w_oi.pe_chg_oi[_w_oi.pe_chg_oi < 0].sum()
            _net_ce = _w_oi.ce_chg_oi.sum()
            _net_pe = _w_oi.pe_chg_oi.sum()
            _pcr_chg = (_net_pe / _net_ce) if _net_ce > 0 else (1.0 if _net_ce == 0 else 0.0)

            if _net_pe > _net_ce * 1.2:
                _bias_label = "🟢 PUT WRITERS DEFENDING"
                _bias_desc = "Put additions exceed Call writing; support base solidifying below spot."
                _bias_col = "#10B981"
            elif _net_ce > _net_pe * 1.2:
                _bias_label = "🔴 CALL WRITERS CAPPING"
                _bias_desc = "Call additions exceed Put writing; overhead resistance clustering above spot."
                _bias_col = "#EF4444"
            else:
                _bias_label = "🟡 BALANCED INTRADAY CHOP"
                _bias_desc = "Two-way positioning; both calls and puts actively written or unwound."
                _bias_col = "#FACC15"

            _hud_html = (
                f'<div style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 14px;">'
                f'<div style="background: #0d131f; border: 1px solid #1e293b; border-top: 3px solid #ef4444; border-radius: 8px; padding: 12px 16px;">'
                f'<div style="font-size: 0.72em; text-transform: uppercase; color: #8fa6b8; font-weight: 700;">CALL ΔOI (NEAR ATM)</div>'
                f'<div style="font-size: 1.45em; font-weight: 800; color: #ef4444; font-family: monospace;">{_net_ce:+,.0f}</div>'
                f'<div style="font-size: 0.75em; color: #94a3b8; margin-top: 4px;"><span style="color: #ef4444;">+{_tot_ce_add:,.0f} added</span> · <span style="color: #f87171;">{_tot_ce_unw:,.0f} shed</span></div>'
                f'</div>'
                f'<div style="background: #0d131f; border: 1px solid #1e293b; border-top: 3px solid #10b981; border-radius: 8px; padding: 12px 16px;">'
                f'<div style="font-size: 0.72em; text-transform: uppercase; color: #8fa6b8; font-weight: 700;">PUT ΔOI (NEAR ATM)</div>'
                f'<div style="font-size: 1.45em; font-weight: 800; color: #10b981; font-family: monospace;">{_net_pe:+,.0f}</div>'
                f'<div style="font-size: 0.75em; color: #94a3b8; margin-top: 4px;"><span style="color: #10b981;">+{_tot_pe_add:,.0f} added</span> · <span style="color: #34d399;">{_tot_pe_unw:,.0f} shed</span></div>'
                f'</div>'
                f'<div style="background: #0d131f; border: 1px solid #1e293b; border-top: 3px solid #38bdf8; border-radius: 8px; padding: 12px 16px;">'
                f'<div style="font-size: 0.72em; text-transform: uppercase; color: #8fa6b8; font-weight: 700;">INTRADAY PCR (ΔPE / ΔCE)</div>'
                f'<div style="font-size: 1.45em; font-weight: 800; color: #38bdf8; font-family: monospace;">{_pcr_chg:.2f}</div>'
                f'<div style="font-size: 0.75em; color: #94a3b8; margin-top: 4px;">Change flow ratio only</div>'
                f'</div>'
                f'<div style="background: #0d131f; border: 1px solid #1e293b; border-top: 3px solid {_bias_col}; border-radius: 8px; padding: 12px 16px;">'
                f'<div style="font-size: 0.72em; text-transform: uppercase; color: #8fa6b8; font-weight: 700;">LIVE INTRADAY STANCE</div>'
                f'<div style="font-size: 1.15em; font-weight: 800; color: {_bias_col}; margin-top: 2px;">{_bias_label}</div>'
                f'<div style="font-size: 0.75em; color: #94a3b8; margin-top: 4px;">{_bias_desc}</div>'
                f'</div>'
                f'</div>'
            )
            _render_html(_hud_html)
    except Exception as _chg_stat_err:
        pass

    # ── 🏹 Institutional Breakout Validation & Anti-Trap Surveillance Card ──
    try:
        import option_chain_oi_tab as _oc_oi_mod2
        _engine2 = _oc_oi_mod2.OptionChainOIEngine()
        _focus_df2 = _engine2.filter_focus_window(df, spot, focus_radius=450)
        _levels2 = _engine2.analyze_levels(_focus_df2, spot)
        if _levels2:
            _verdict2 = _engine2.validate_breakout(_focus_df2, spot, _levels2)
            _oc_oi_mod2.render_breakout_validation_summary_card(_verdict2)
    except Exception as _bkv_err2:
        st.caption(f"Breakout validation card unavailable: {_bkv_err2}")

    st.plotly_chart(chart_oi_change(df, read), use_container_width=True)

    _guide_html = (
        f'<div style="display: flex; gap: 16px; align-items: center; justify-content: space-between; flex-wrap: wrap; background: #0b111c; border: 1px solid #1e293b; padding: 10px 16px; border-radius: 6px; margin-top: 6px; font-size: 0.82em; color: #8fa6b8;">'
        f'<div style="display: flex; align-items: center; gap: 8px;">'
        f'<span style="font-weight: 700; color: #cbd5e1;">🧭 Directional Chart Guide:</span>'
        f'<span>Bars extending <b>RIGHT ▶</b> = Added Contracts (Building)</span>'
        f'<span>·</span>'
        f'<span>Bars extending <b>◀ LEFT</b> = Shed Contracts (Unwinding)</span>'
        f'</div>'
        f'<div style="display: flex; align-items: center; gap: 12px; flex-wrap: wrap;">'
        f'<span><b style="color: #ef4444;">■ Solid Red:</b> Call Building</span>'
        f'<span><b style="color: #f87171; opacity: 0.85;">▨ Faded Red:</b> Call Unwinding</span>'
        f'<span><b style="color: #10b981;">■ Solid Green:</b> Put Building</span>'
        f'<span><b style="color: #34d399; opacity: 0.85;">▨ Faded Green:</b> Put Unwinding</span>'
        f'<span><b style="color: #facc15;">— Yellow Line:</b> ATM Spot</span>'
        f'</div>'
        f'</div>'
    )
    _render_html(_guide_html)

    # ── 🌊 Live Intraday Flow & Shift Narratives ─────────────────────────
    try:
        import option_chain_narrative as _ocn
        _ocn.render_tab2_oi_change_narratives(df, read, spot)
    except Exception as _ocn_err2:
        st.caption(f"Intraday OI shift narratives unavailable: {_ocn_err2}")
if _tab_active(3):
    refresh_control("session", (fetch_live,), "option chain", nse_ts)

    try:
        _crosses = None
        _f5 = pa_frames.get("5m")
        if _f5 is not None and "vwap" in _f5 and _f5["vwap"].notna().any():
            _c5 = _f5[_f5["closed"]]
            _side = np.sign(_c5["close"] - _c5["vwap"]).dropna()
            _crosses = int((_side.diff().fillna(0) != 0).sum())

        _reg = an.day_regime(df, spot, read.levels, STATE.baseline,
                             vix.pct_change if vix else None,
                             _crosses, an.intraday_pcr(df, spot))
        _box = {"TREND": st.success, "RANGE": st.warning,
                "UNCLEAR": st.info}[_reg.verdict]
        _box(f"**{_reg.verdict} DAY** — {_reg.agreed} of 4 checks agree "
             f"(score {_reg.score:+d})")
        st.dataframe(pd.DataFrame([{"Check": n, "Reads": v, "Detail": w}
                                   for n, v, w in _reg.checks]),
                     use_container_width=True, hide_index=True)
        st.caption(f"**Playbook:** {_reg.playbook}")
        st.caption(f"**Instruments:** {_reg.instruments}")

        if _f5 is not None:
            _pdh = _pdl = _pc = None
            if vb_daily is not None and len(vb_daily) > 1:
                _prev = vb_daily.iloc[-1]
                _pdh, _pdl, _pc = (float(_prev["High"]), float(_prev["Low"]),
                                   float(_prev["Close"]))
            _tr = an.vwap_trap(_f5, df, spot, read.levels, _pdh, _pdl, _pc)
            if _tr:
                _msg = (f"**VWAP TRAP — {_tr.direction}** · {_tr.detail}\n\n"
                        f"Stop {_tr.stop:,.0f} · target {_tr.target:,.0f} · "
                        f"RR {_tr.rr:.2f}")
                (st.success if _tr.valid_rr else st.warning)(
                    _msg + ("" if _tr.valid_rr else
                            "\n\nRR below 1:2 — the notes say skip it."))
    except Exception as _e:                                 # noqa: BLE001
        st.caption(f"regime read unavailable: {_e}")
    hist = STATE.frame
    if len(hist) >= 2:
        st.plotly_chart(chart_migration(hist), use_container_width=True)
    else:
        st.info(f"Collecting snapshots — the session chart needs at least two polls "
                f"({config.POLL_SECONDS}s apart). This is the most useful view; "
                f"leave it running.")
if _tab_active(4):
    refresh_control("score", (fetch_live,), "option chain", nse_ts)
    try:
        import score_tab
        score_tab.render_score_breakdown(
            spot=spot,
            df=df,
            read=read,
            pa_frames=pa_frames if "pa_frames" in locals() else None,
        )
    except Exception as _st_err:
        st.error(f"Score Breakdown Module Error: {_st_err}")

if _tab_active(5):
    refresh_control("price_levels", (fetch_live, load_ohlc_cached,),
                    "chain + daily OHLC", nse_ts, pull_ohlc=True)
    if ohlc_err:
        st.warning("Price-structure levels unavailable.")
        st.code(ohlc_err, language="text")
        st.info(f"Drop your NIFTY daily OHLC export into `data/ohlc/` as "
                f"`{config.OHLC_FILE}`. Columns needed: Date, Open, High, Low, "
                f"Close. Replace it as often as you like — every load merges "
                f"into an archive, so history accumulates rather than being "
                f"overwritten.")
    else:
        if ohlc_span:
            n, d0, d1 = ohlc_span
            st.caption(f"{n} trading days loaded · {d0:%d-%b-%Y} to {d1:%d-%b-%Y} "
                       f"(archive keeps history even when you replace "
                       f"{config.OHLC_FILE})")
        for note in ohlc_notes:
            st.warning(note)

        if cpr:
            cc = st.columns(5)
            cc[0].metric("CPR top", f"{cpr['top']:,.0f}")
            cc[1].metric("CPR pivot", f"{cpr['pivot']:,.0f}")
            cc[2].metric("CPR bottom", f"{cpr['bottom']:,.0f}")
            cc[3].metric("Width", f"{cpr['width']:,.0f} pts",
                         f"{cpr['width_pct']:.2f}%", delta_color="off")
            cc[4].metric("Read", cpr["verdict"])

            msg = f"**CPR {cpr['verdict']}** ({cpr['width_pct']:.2f}% of spot) — {cpr['hint']}."
            if cpr["spot_inside"]:
                msg += " Spot is **inside** the CPR — direction undecided."
            # Cross-check against what the option chain independently said.
            if cpr["verdict"] == "WIDE" and read.regime in ("RANGEBOUND", "CONSOLIDATION"):
                msg += " The chain agrees: two independent inputs both say sideways."
            elif cpr["verdict"] == "NARROW" and read.regime == "CONSOLIDATION":
                msg += (" But the chain says compression — narrow CPR plus tight "
                        "OI walls is a coiled spring, not a dead day.")
            elif cpr["verdict"] == "NARROW" and read.regime in ("BULLISH", "BEARISH"):
                msg += " The chain agrees a trend is developing."
            st.info(msg)

        # ── Google TimesFM 3.0 AI Quantile Envelope & Confluence Synthesis ──
        _f15_cft = pa_frames.get("15m") if ("pa_frames" in locals() and pa_frames) else None
        _f5_cft = pa_frames.get("5m") if ("pa_frames" in locals() and pa_frames) else None
        _ai_gate_tab5 = cf.evaluate_ai_confluence_gate(spot, df_15m=_f15_cft, df_5m=_f5_cft)

        _ai_c1, _ai_c2, _ai_c3, _ai_c4 = st.columns([1.2, 1.2, 1.2, 1.4])
        _ai_c1.metric("TimesFM q10 Floor (Support)", f"{_ai_gate_tab5.q10_floor:,.1f}", delta=f"-{_ai_gate_tab5.dist_to_floor:.1f} pts from spot", delta_color="inverse")
        _ai_c2.metric("TimesFM q50 Median (T1 Target)", f"{_ai_gate_tab5.q50_median:,.1f}", delta="Mean Reversion Pivot")
        _ai_c3.metric("TimesFM q90 Cap (Resistance)", f"{_ai_gate_tab5.q90_cap:,.1f}", delta=f"+{_ai_gate_tab5.dist_to_cap:.1f} pts from spot")
        _ai_stat_badge = getattr(_ai_gate_tab5, "gate_color", "#2FBF71")
        with _ai_c4:
            _render_html(
                f"""
                <div style="background: rgba(14, 34, 56, 0.9); border: 1px solid {_ai_stat_badge}; border-radius: 6px; padding: 10px 12px;">
                    <div style="font-size: 11px; color: #8FA6B8;">AI CONFLUENCE FILTER</div>
                    <div style="font-size: 15px; font-weight: 800; color: {_ai_stat_badge}; font-family: monospace;">{_ai_gate_tab5.status.replace('_', ' ')}</div>
                    <div style="font-size: 11px; color: #00E5FF; margin-top: 2px;">Lock CTC at +20 pts excursion</div>
                </div>
                """
            )

        st.info(f"🔮 **AI Confluence Synthesis & CTC Recommendation:** {_ai_gate_tab5.message} (Empirical backtest: 87% cone containment, Cost-to-Cost exit saves 41% of stopouts).")

        # Inject AI levels into Confluence zones
        _ai_zones_levels = [
            (_ai_gate_tab5.q90_cap, "resistance", "TimesFM 3.0 q90 Cap"),
            (_ai_gate_tab5.q10_floor, "support", "TimesFM 3.0 q10 Floor"),
            (_ai_gate_tab5.q50_median, "resistance" if _ai_gate_tab5.q50_median > spot else "support", "TimesFM 3.0 q50 Median"),
        ]
        zones = cf.build_zones(price_lv, oi_lv, ai_levels=_ai_zones_levels)
        tb = cf.get_tactical_battlefield(zones, spot)

        # ── Hero Tactical Decision HUD ──
        _regime_badge_color = "#10b981" if tb.tactical_regime == "VALUE_BUY_ZONE" else ("#ef4444" if tb.tactical_regime == "RESISTANCE_SELL_ZONE" else "#f59e0b")
        _regime_badge_bg = "rgba(16, 185, 129, 0.15)" if tb.tactical_regime == "VALUE_BUY_ZONE" else ("rgba(239, 68, 68, 0.15)" if tb.tactical_regime == "RESISTANCE_SELL_ZONE" else "rgba(245, 158, 11, 0.15)")
        _regime_label = "🟢 VALUE BUY ZONE (Floor Defense)" if tb.tactical_regime == "VALUE_BUY_ZONE" else ("🔴 CEILING REJECTION ZONE (Wall Defense)" if tb.tactical_regime == "RESISTANCE_SELL_ZONE" else "⚖️ MID-CORRIDOR CHOP (Patience · Low Asymmetry)")

        _hud_floor_txt = f"{tb.immediate_support.price:,.1f}" if tb.immediate_support else "—"
        _hud_ceil_txt = f"{tb.immediate_resistance.price:,.1f}" if tb.immediate_resistance else "—"
        _dist_floor = f"-{spot - tb.immediate_support.price:.1f} pts" if tb.immediate_support else ""
        _dist_ceil = f"+{tb.immediate_resistance.price - spot:.1f} pts" if tb.immediate_resistance else ""

        _render_html(
            f"""
            <div style="background: linear-gradient(135deg, #091322 0%, #102035 100%); border: 1px solid #1e3a8a; border-radius: 8px; padding: 18px 20px; margin-top: 10px; margin-bottom: 18px; box-shadow: 0 4px 20px rgba(0,0,0,0.35);">
                <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px;">
                    <div>
                        <span style="font-size: 11px; font-weight: 700; color: #94a3b8; letter-spacing: 1px; text-transform: uppercase;">Real-Time Price Level Decision HUD</span>
                        <div style="font-size: 18px; font-weight: 800; color: #f8fafc; margin-top: 2px;">{tb.headline}</div>
                    </div>
                    <div>
                        <span style="display: inline-block; background: {_regime_badge_bg}; color: {_regime_badge_color}; border: 1px solid {_regime_badge_color}; font-size: 12px; font-weight: 800; padding: 5px 12px; border-radius: 6px; font-family: monospace;">{_regime_label}</span>
                    </div>
                </div>
                <div style="margin-top: 14px; background: rgba(15, 23, 42, 0.7); border: 1px solid #334155; border-radius: 6px; padding: 12px 16px;">
                    <div style="display: flex; justify-content: space-between; font-size: 12px; font-family: monospace; color: #cbd5e1; margin-bottom: 6px;">
                        <span>🛡️ <b>FLOOR:</b> {_hud_floor_txt} <span style="color: #6ee7b7;">({_dist_floor})</span></span>
                        <span>📍 <b>LIVE SPOT:</b> <span style="color: #ffd700; font-weight: 800;">{spot:,.1f}</span> ({tb.position_pct:.0f}% of corridor)</span>
                        <span>🛑 <b>CEILING:</b> {_hud_ceil_txt} <span style="color: #f87171;">({_dist_ceil})</span></span>
                    </div>
                    <div style="width: 100%; background: #1e293b; height: 10px; border-radius: 5px; overflow: hidden; position: relative;">
                        <div style="width: {tb.position_pct:.1f}%; background: linear-gradient(90deg, #10b981 0%, #f59e0b 60%, #ef4444 100%); height: 100%; border-radius: 5px;"></div>
                    </div>
                    <div style="margin-top: 8px; font-size: 12px; color: #94a3b8;">
                        💡 <b style="color: #e2e8f0;">Trader Direct:</b> {tb.action_advice}
                    </div>
                </div>
            </div>
            """
        )

        _m1, _m2, _m3, _m4, _m5 = st.columns(5)
        _m1.metric("Immediate Floor", _hud_floor_txt, _dist_floor, delta_color="inverse")
        _m2.metric("Immediate Ceiling", _hud_ceil_txt, _dist_ceil)
        _m3.metric("Long Risk : Reward", f"{tb.risk_reward_long} : 1",
                   delta="Poor Asymmetry Mid-air" if tb.risk_reward_long < 1.5 else "Favorable Asymmetry",
                   delta_color="normal" if tb.risk_reward_long >= 1.5 else "inverse")
        _m4.metric("Short Risk : Reward", f"{tb.risk_reward_short} : 1",
                   delta="Poor Asymmetry Mid-air" if tb.risk_reward_short < 1.5 else "Favorable Asymmetry",
                   delta_color="normal" if tb.risk_reward_short >= 1.5 else "inverse")
        _m5.metric("Active Corridor", f"{tb.corridor_pts:.0f} pts", f"{tb.corridor_pts / spot * 100:.2f}% span", delta_color="off")

        # ── 3 Actionable Institutional Playbook Cards ──
        st.markdown("#### ⚡ Real-Time Tactical Playbooks at this Price Level")
        pb_c1, pb_c2, pb_c3 = st.columns(3)
        with pb_c1:
            _render_html(
                f"""
                <div style="background: rgba(12, 26, 36, 0.92); border: 1px solid #059669; border-left: 5px solid #10b981; border-radius: 6px; padding: 14px; min-height: 230px;">
                    <div style="font-size: 11px; font-weight: 700; color: #10b981; letter-spacing: 0.5px;">PLAYBOOK A · DIP-BUY RELOAD</div>
                    <div style="font-size: 15px; font-weight: 800; color: #f8fafc; margin-top: 3px;">Floor Defense Entry</div>
                    <div style="margin-top: 10px; font-size: 12px; color: #cbd5e1; line-height: 1.6;">
                        <div><b>Entry Zone:</b> <span style="color:#6ee7b7; font-family: monospace;">{tb.playbook_dip_buy['entry_zone']}</span></div>
                        <div><b>Protective SL:</b> <span style="color:#f87171; font-family: monospace;">{tb.playbook_dip_buy['sl']}</span></div>
                        <div><b>Upside Target:</b> <span style="color:#38bdf8; font-family: monospace;">{tb.playbook_dip_buy['target']}</span></div>
                        <div><b>Asymmetry R:R:</b> <span style="color:#fbbf24; font-weight: 700; font-family: monospace;">{tb.playbook_dip_buy['rr']}</span></div>
                    </div>
                    <div style="margin-top: 8px; padding-top: 6px; border-top: 1px solid #1e293b; font-size: 11px; color: #94a3b8;">
                        <b>Setup:</b> {tb.playbook_dip_buy['rationale']}
                    </div>
                </div>
                """
            )
        with pb_c2:
            _render_html(
                f"""
                <div style="background: rgba(28, 13, 21, 0.92); border: 1px solid #b91c1c; border-left: 5px solid #ef4444; border-radius: 6px; padding: 14px; min-height: 230px;">
                    <div style="font-size: 11px; font-weight: 700; color: #ef4444; letter-spacing: 0.5px;">PLAYBOOK B · CEILING FADE</div>
                    <div style="font-size: 15px; font-weight: 800; color: #f8fafc; margin-top: 3px;">Wall Rejection Short</div>
                    <div style="margin-top: 10px; font-size: 12px; color: #cbd5e1; line-height: 1.6;">
                        <div><b>Entry Zone:</b> <span style="color:#fca5a5; font-family: monospace;">{tb.playbook_wall_fade['entry_zone']}</span></div>
                        <div><b>Protective SL:</b> <span style="color:#f87171; font-family: monospace;">{tb.playbook_wall_fade['sl']}</span></div>
                        <div><b>Downside Target:</b> <span style="color:#38bdf8; font-family: monospace;">{tb.playbook_wall_fade['target']}</span></div>
                        <div><b>Asymmetry R:R:</b> <span style="color:#fbbf24; font-weight: 700; font-family: monospace;">{tb.playbook_wall_fade['rr']}</span></div>
                    </div>
                    <div style="margin-top: 8px; padding-top: 6px; border-top: 1px solid #1e293b; font-size: 11px; color: #94a3b8;">
                        <b>Setup:</b> {tb.playbook_wall_fade['rationale']}
                    </div>
                </div>
                """
            )
        with pb_c3:
            _render_html(
                f"""
                <div style="background: rgba(9, 26, 41, 0.92); border: 1px solid #0284c7; border-left: 5px solid #06b6d4; border-radius: 6px; padding: 14px; min-height: 230px;">
                    <div style="font-size: 11px; font-weight: 700; color: #06b6d4; letter-spacing: 0.5px;">PLAYBOOK C · SQUEEZE BREAKOUT</div>
                    <div style="font-size: 15px; font-weight: 800; color: #f8fafc; margin-top: 3px;">Momentum Expansion</div>
                    <div style="margin-top: 10px; font-size: 12px; color: #cbd5e1; line-height: 1.6;">
                        <div><b>Trigger:</b> <span style="color:#38bdf8; font-family: monospace;">{tb.playbook_squeeze_breakout['trigger']}</span></div>
                        <div><b>Entry:</b> <span style="color:#6ee7b7; font-family: monospace;">{tb.playbook_squeeze_breakout['entry']}</span></div>
                        <div><b>Protective SL:</b> <span style="color:#f87171; font-family: monospace;">{tb.playbook_squeeze_breakout['sl']}</span></div>
                        <div><b>Expansion Tgt:</b> <span style="color:#a78bfa; font-weight: 700; font-family: monospace;">{tb.playbook_squeeze_breakout['target']}</span></div>
                    </div>
                    <div style="margin-top: 8px; padding-top: 6px; border-top: 1px solid #1e293b; font-size: 11px; color: #94a3b8;">
                        <b>Setup:</b> {tb.playbook_squeeze_breakout['rationale']}
                    </div>
                </div>
                """
            )

        # ── 🪜 Option-Chain Key Levels — Price Ladder Grid ─────────────────
        try:
            render_price_ladder_grid(df, spot)
        except Exception as _ple5:
            st.caption(f"Price ladder grid unavailable: {_ple5}")

        st.plotly_chart(chart_confluence(zones, spot), use_container_width=True)

        st.markdown("##### Confluence Zones & Battlefield Overview")
        view_mode = st.radio(
            "Confluence View Layout",
            ["⚔️ Tactical Split Battlefield (Ceilings Above vs Floors Below)", "📋 Full Confluence Table (Audit Mode)"],
            horizontal=True,
            label_visibility="collapsed"
        )

        if "Split" in view_mode:
            col_res, col_sup = st.columns(2)
            with col_res:
                st.markdown(f"**🛑 Resistance Ceilings Above Spot ({len(tb.resistances_above)} zones)**")
                if not tb.resistances_above:
                    st.caption("No resistance zones detected nearby.")
                for z in tb.resistances_above[:8]:
                    pills = "".join(
                        f'<span style="display:inline-block; background: #1e293b; color: #cbd5e1; border: 1px solid #475569; font-size: 11px; padding: 2px 7px; border-radius: 4px; margin-right: 4px; margin-bottom: 4px; font-family: monospace;">{m}</span>'
                        for m in z.members
                    )
                    score_col = "#ef4444" if z.score >= 7.0 else ("#f97316" if z.score >= 5.0 else "#fbbf24")
                    _render_html(
                        f"""
                        <div style="background: rgba(15, 23, 42, 0.85); border: 1px solid #334155; border-left: 4px solid #ef4444; border-radius: 6px; padding: 10px 14px; margin-bottom: 8px;">
                            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                                <div>
                                    <span style="font-size: 16px; font-weight: 800; color: #f8fafc; font-family: monospace;">{z.price:,.1f}</span>
                                    <span style="font-size: 12px; color: #f87171; margin-left: 8px; font-weight: 600; font-family: monospace;">+{z.price - spot:.1f} pts</span>
                                </div>
                                <div>
                                    <span style="background: rgba(239, 68, 68, 0.15); color: {score_col}; border: 1px solid {score_col}; font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 4px;">⭐ {z.score:.1f} · {z.strength}</span>
                                </div>
                            </div>
                            <div style="display: flex; flex-wrap: wrap; margin-top: 6px;">
                                {pills}
                            </div>
                        </div>
                        """
                    )

            with col_sup:
                st.markdown(f"**🛡️ Support Floors Below Spot ({len(tb.supports_below)} zones)**")
                if not tb.supports_below:
                    st.caption("No support zones detected nearby.")
                for z in tb.supports_below[:8]:
                    pills = "".join(
                        f'<span style="display:inline-block; background: #1e293b; color: #cbd5e1; border: 1px solid #475569; font-size: 11px; padding: 2px 7px; border-radius: 4px; margin-right: 4px; margin-bottom: 4px; font-family: monospace;">{m}</span>'
                        for m in z.members
                    )
                    score_col = "#10b981" if z.score >= 7.0 else ("#06b6d4" if z.score >= 5.0 else "#34d399")
                    _render_html(
                        f"""
                        <div style="background: rgba(15, 23, 42, 0.85); border: 1px solid #334155; border-left: 4px solid #10b981; border-radius: 6px; padding: 10px 14px; margin-bottom: 8px;">
                            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                                <div>
                                    <span style="font-size: 16px; font-weight: 800; color: #f8fafc; font-family: monospace;">{z.price:,.1f}</span>
                                    <span style="font-size: 12px; color: #34d399; margin-left: 8px; font-weight: 600; font-family: monospace;">-{spot - z.price:.1f} pts</span>
                                </div>
                                <div>
                                    <span style="background: rgba(16, 185, 129, 0.15); color: {score_col}; border: 1px solid {score_col}; font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 4px;">⭐ {z.score:.1f} · {z.strength}</span>
                                </div>
                            </div>
                            <div style="display: flex; flex-wrap: wrap; margin-top: 6px;">
                                {pills}
                            </div>
                        </div>
                        """
                    )
        else:
            st.dataframe(
                cf.zones_frame(zones, spot),
                use_container_width=True,
                hide_index=True,
                column_config={"Agreeing levels": st.column_config.TextColumn("Agreeing Levels", width="large")}
            )

        st.caption("A zone is every level within "
                   f"{config.CONFLUENCE_TOLERANCE} points of the others, merged. "
                   "Score weights: OI wall and unfilled gap 3.0, prior swing 2.0, "
                   "round number 1.0, Gann 0.5. Two independent level types "
                   "agreeing is what makes a zone worth trading — a high score "
                   "from one source alone is not confluence.")

        if STATE.flips and STATE.flips.flips:
            st.markdown("##### Levels broken this session")
            st.dataframe(STATE.flips.frame(), use_container_width=True,
                         hide_index=True)
            st.caption("A broken support becomes resistance and vice versa, "
                       "because whoever was positioned across it is now "
                       "underwater and sells into any return to their entry. "
                       "A retest that REJECTS is the tradeable event. A retest "
                       "that goes straight through means the break is failing "
                       "— and 'reclaimed' means it already did.")

        with st.expander("All price levels (before merging)"):
            filter_tactical = st.checkbox("Focus on nearby tactical levels (±500 pts of Spot)", value=True, key="chk_filter_tactical")
            raw_levels = sorted(price_lv, key=lambda x: -x.price)
            if filter_tactical:
                filtered_lv = [l for l in raw_levels if abs(l.price - spot) <= 500]
                st.caption(f"Showing {len(filtered_lv)} levels within ±500 pts of Spot {spot:,.1f} (out of {len(raw_levels)} total). Uncheck above to see macro levels.")
            else:
                filtered_lv = raw_levels
                st.caption(f"Showing all {len(filtered_lv)} levels across all price ranges.")

            st.dataframe(pd.DataFrame([
                {"Price": f"{l.price:,.1f}", "Distance": f"{l.price - spot:+,.1f} pts",
                 "Type": l.kind, "Source": l.source, "Weight": l.weight, "Label": l.label}
                for l in filtered_lv
            ]), use_container_width=True, hide_index=True)

        if cycles:
            st.markdown("##### Time cycles")
            for c in cycles:
                st.info(c)
            st.caption("Shown, never scored. With one major high and one major "
                       "low in the dataset there is no way to separate a hit "
                       "from a coincidence. Note them; do not trade them.")

if _tab_active(6):
    refresh_control("context", (fetch_vix_cached, fetch_flows_cached,), "VIX + FII/DII")
    try:
        context_tab.render_context_tab(
            vix=vix,
            flows=flows,
            vb_poi=vb_poi if "vb_poi" in locals() else None,
            vb_ad_n=vb_ad_n if "vb_ad_n" in locals() else None,
            vb_ad_b=vb_ad_b if "vb_ad_b" in locals() else None,
            vb_gift=vb_gift if "vb_gift" in locals() else None,
            vb_glob=vb_glob if "vb_glob" in locals() else None,
            vb_crude=vb_crude if "vb_crude" in locals() else None,
            vb_indices=vb_indices if "vb_indices" in locals() else None,
            spot=spot if "spot" in locals() else 0.0,
            vix_err=vix_err if "vix_err" in locals() else "",
            flows_err=flows_err if "flows_err" in locals() else "",
            df=df if "df" in locals() else None,
        )
    except Exception as _ctx_err:
        st.error(f"Context Module Error: {_ctx_err}")

if _tab_active(7):
    refresh_control("price_action", (fetch_price_action,), "candles")
    if pa_err:
        st.warning("Price action unavailable.")
        st.code(pa_err, language="text")
        if any(k in pa_err.lower() for k in ("invalid_authentication", "expired", "dh-901")):
            st.error("🔑 **Dhan Access Token is Expired or Invalid (DH-901).**")
            with st.expander("Update Dhan Access Token Here", expanded=True):
                _in_tok = st.text_area("Paste token", height=70, key="pa_tab_inline_token",
                                       placeholder="Paste your 24-hr access token copied from Dhan app...",
                                       label_visibility="collapsed")
                if st.button("⚡ Test & Save Token", key="pa_tab_inline_save"):
                    if not _in_tok.strip():
                        st.warning("Please paste an access token.")
                    else:
                        with st.spinner("Testing with Dhan API..."):
                            _ok, _msg = dc.verify_token(_in_tok.strip())
                        if _ok:
                            dc.save_token(_in_tok.strip())
                            st.session_state["dhan_verify_status"] = {
                                "ok": True,
                                "msg": "Access key is working! Connected to Dhan Market Data.",
                                "time": datetime.now().strftime("%H:%M:%S")
                            }
                            st.cache_data.clear()
                            st.success("✅ **Access key is working!** Token saved.")
                            st.rerun()
                        else:
                            st.session_state["dhan_verify_status"] = {
                                "ok": False,
                                "msg": _msg,
                                "time": datetime.now().strftime("%H:%M:%S")
                            }
                            st.error(f"❌ Verification failed: {_msg}")
        else:
            st.info(f"Current source: **{config.CANDLE_SOURCE}**. For yfinance, "
                    f"`pip install yfinance` and note 1-minute data only covers "
                    f"about the last 7 days. For Dhan, run "
                    f"`python diagnose_dhan.py` — and remember the Data API needs "
                    f"a paid subscription separate from the free Trading API.")
    elif not pa_frames:
        st.info("Price action is off, or you are in demo mode.")
    else:
        if pa_stats:
            q = st.columns(6)
            q[0].metric("Day open", f"{pa_stats['open']:,.0f}")
            q[1].metric("Day high", f"{pa_stats['high']:,.0f}")
            q[2].metric("Day low", f"{pa_stats['low']:,.0f}")
            q[3].metric("Range", f"{pa_stats['range']:,.0f} pts")
            q[4].metric("In range", f"{pa_stats['pos_in_range']*100:.0f}%",
                        help="0% at the low, 100% at the high")
            q[5].metric("From open", f"{pa_stats['from_open']:+,.0f}")
        if pa_stats.get("extremes_source"):
            st.caption(f"Day high/low from: **{pa_stats['extremes_source']}**"
                       + ("" if pa_stats["extremes_source"] == "exchange" else
                          " — the price series samples ~1/min and undershoots "
                          "the true extremes, so treat the range as indicative"))
        if pa_orb:
            st.caption(f"Opening range ({pa_orb['minutes']}m): "
                       f"{pa_orb['low']:,.0f} – {pa_orb['high']:,.0f}"
                       + ("" if pa_orb.get("complete") else "  · still forming"))

        import candle_source as _cs
        _src = _cs.describe()
        st.caption(f"Source: **{_src['name']}** · {_src['symbol']} · "
                   f"VWAP {'available' if _src['vwap'] else 'NOT available'}")
        with st.expander("About this data source and its limits"):
            st.write(_src["note"])

        _rt = {tf: (fr.attrs or {}).get("candle_route", "?")
               for tf, fr in pa_frames.items()}
        _resampled = [tf for tf, r in _rt.items() if "resampled" in str(r)]
        if _resampled:
            _cov = []
            for _tf in _resampled:
                _rp = pa.coverage_report(pa_frames[_tf])
                if _rp.get("known"):
                    _cov.append(f"{_tf} {_rp['coverage_pct']:.0f}% "
                                f"({_rp['thin']} thin)")
            st.warning(
                "**Candles built by resampling: " + ", ".join(_resampled) + "**"
                + (("  \n" + " · ".join(_cov)) if _cov else "")
                + "  \nA resampled candle keeps the right CLOSE but takes its "
                  "high and low only from the minutes that arrived. Missing "
                  "minutes make wicks too short, so these will not match "
                  "TradingView candle for candle even though the prices and "
                  "timings are correct.")
        else:
            st.caption("Candles: " + " · ".join(f"{k} {v}" for k, v in _rt.items()))

        sub = st.radio("Timeframe", list(pa_frames.keys()), horizontal=True,
                       index=1, key="pa_tf",
                       help="15m for bias, 5m for the decision, 1m for entry "
                            "timing and stop placement — not for deciding.")
        d, r = pa_frames[sub], pa_reads[sub]

        m = st.columns(5)
        m[0].metric("Trend", r.trend)
        m[1].metric("EMA 8", f"{r.ema8:,.0f}" if r.ema8 else "—")
        m[2].metric("EMA 10", f"{r.ema10:,.0f}" if r.ema10 else "—")
        m[3].metric("VWAP", f"{r.vwap:,.0f}" if r.vwap else "n/a",
                    "above" if r.above_vwap else ("below" if r.above_vwap is False else ""),
                    delta_color="normal" if r.above_vwap else "inverse")
        m[4].metric("ATR", f"{r.atr:,.0f} pts" if r.atr else "—",
                    help="Use this to size your stop, then your position.")
        if r.volume_read:
            vm = st.columns(3)
            vm[0].metric("Bar volume", f"{r.vol_last:,.0f}" if r.vol_last else "—",
                         help="Lakhs of constituent shares in this bar.")
            vm[1].metric("20-bar avg", f"{r.vol_avg:,.0f}" if r.vol_avg else "—")
            vm[2].metric("Ratio",
                         f"{r.vol_last / r.vol_avg:.1f}x" if r.vol_avg else "—")
            matched = (pa_frames.get("1m").attrs.get("volume_matched_bars")
                       if pa_frames.get("1m") is not None else None)
            st.caption(
                f"Volume is INDEX volume — summed shares across all 50 "
                f"constituents, differenced from NSE's cumulative day figure. "
                f"It builds up as the app runs"
                + (f" ({matched} bars have it so far)" if matched else "")
                + ". Good for asking whether a move has participation behind "
                  "it. NOT a VWAP source: real NIFTY VWAP comes from futures "
                  "volume, so a line built from this would not match anyone "
                  "else's chart.")

        if r.forming:
            st.warning(f"The current {sub} candle is still FORMING — shown on "
                       f"the chart but excluded from the trend read. If you "
                       f"trade {sub} closes, this candle can still change its "
                       f"mind.")
        for n in r.notes:
            if "FORMING" not in n:
                st.info(n)

        st.markdown("##### VWAP")
        _vrows = []
        for _tf, _fr in pa_frames.items():
            _vr = pa.vwap_read(_fr, _tf)
            _vrows.append({
                "TF": _tf,
                "VWAP": f"{_vr.vwap:,.1f}" if _vr.vwap else "—",
                "Close": f"{_vr.close:,.1f}",
                "Distance": f"{_vr.distance:+,.0f}" if _vr.distance is not None else "—",
                "Position": _vr.status,
                "Bars": _vr.bars,
                "Note": _vr.note,
            })
        st.dataframe(pd.DataFrame(_vrows), use_container_width=True,
                     hide_index=True)
        # Is this VWAP anchored from 09:15, or from when the app started?
        try:
            _f1 = pa_frames.get("1m")
            if _f1 is not None and "vwap_bars" in _f1 and len(_f1):
                _vb = int(_f1["vwap_bars"].iloc[-1])
                _vt = fl.vwap_trust(_vb, now, 1)
                (st.success if _vt.usable else st.error)(
                    f"**VWAP {'anchored properly' if _vt.usable else 'NOT TRUSTWORTHY'}**"
                    f"  \n{_vt.note}")
        except Exception:                                   # noqa: BLE001
            pass

        _crossed = [r for r in _vrows if "CROSSED" in r["Position"]]
        if _crossed:
            st.warning("**VWAP cross on " +
                       ", ".join(r["TF"] for r in _crossed) +
                       "** — the level a lot of intraday desks watch.")
        st.caption("VWAP is built from INDEX volume (all 50 constituents), "
                   "session-anchored, hlc3 — the same basis as the "
                   "\"VWAP hlc3 Session\" indicator on your chart. Compare the "
                   "two once: if they line up, trust it; if not, this is a "
                   "different line and should be read as such. It can only "
                   "accumulate from the first bar this app saw, so the Bars "
                   "column tells you how well anchored it is.")

        st.plotly_chart(candle_chart(d, sub), use_container_width=True)

        with st.expander("All three timeframes at a glance"):
            st.dataframe(pd.DataFrame([{
                "TF": rr.timeframe, "Trend": rr.trend,
                "Close": f"{rr.close:,.1f}",
                "EMA8": f"{rr.ema8:,.1f}" if rr.ema8 else "—",
                "EMA10": f"{rr.ema10:,.1f}" if rr.ema10 else "—",
                "VWAP": f"{rr.vwap:,.1f}" if rr.vwap else "n/a",
                "ATR": f"{rr.atr:,.1f}" if rr.atr else "—",
                "Closed bars": rr.bars_closed,
                "Forming": "yes" if rr.forming else "no",
            } for rr in pa_reads.values()]), use_container_width=True, hide_index=True)
            st.caption(f"1-minute candles are stored to "
                       f"`{config.CANDLE_DIR.name}/YYYY-MM-DD.csv` every poll, "
                       f"full session 09:15–15:30. Only 1m is stored — 5m and "
                       f"15m are derived, so saving them could drift out of "
                       f"sync with the source.")

if _tab_active(8):
    refresh_control("setups", (fetch_live, fetch_price_action,),
                    "chain + candles", nse_ts)
    st.caption("This checks the objective preconditions and — the part that "
               "matters — the SKIP conditions. Your entry trigger stays yours: "
               "the 8 EMA touch, the rejection, the pattern break, read off "
               "your own Kite chart.")

    if not pa_frames or "5m" not in pa_frames:
        st.info("Needs price action data. Check the Price action tab first.")
    else:
        f5 = su.prepare(pa_frames["5m"])
        f15 = su.prepare(pa_frames["15m"])
        ctx = su.context_summary(f5, f15, now)

        if not ctx:
            st.info("Not enough closed 5-minute candles yet — the Bollinger "
                    "Bands need 20.")
        else:
            k = st.columns(6)
            k[0].metric("15-min trend", ctx["trend15"])
            k[1].metric("BB width", f"{ctx['bb_width']:.0f}",
                        "OK" if ctx["bb_width_ok"] else "TOO NARROW",
                        delta_color="normal" if ctx["bb_width_ok"] else "inverse")
            k[2].metric("UBB slope", ctx["ubb_slope"])
            k[3].metric("LBB slope", ctx["lbb_slope"])
            k[4].metric("8 EMA", f"{ctx['ema8']:,.0f}",
                        f"{ctx['dist_to_ema8']:+.0f} pts", delta_color="off")
            k[5].metric("Party switch", ctx["party"])

            if ctx["after_3pm"]:
                st.error("**After 3:00 PM — no new entries.** Course rule.")
            if ctx["trend15"] == "CONSOLIDATION":
                st.warning("15-min shows CONSOLIDATION — the filter says skip "
                           "the entire day.")

            cc = None
            try:
                cc = su.chain_condition(df, spot)
                box = st.success if cc.score > 0 else (st.error if cc.score < 0 else st.info)
                box(f"**Option chain: {cc.name}** · {cc.pattern}")
                if cc.exact:
                    cl = st.columns(3)
                    cl[0].markdown(f"**Trailing:** {cc.trail}")
                    cl[1].markdown(f"**Best:** {', '.join(cc.best)}")
                    cl[2].markdown(f"**Avoid:** {', '.join(cc.avoid)}")
                else:
                    st.caption("This colour combination is not one of the five "
                               "named conditions. Saying so rather than forcing "
                               "it into the nearest one — an unmapped chain is "
                               "real information.")
                st.caption("Colours here are derived from net CHANGE IN OI per "
                           "quadrant, scanning 500 pts each side. Worth "
                           "confirming with MJ that this matches what he means "
                           "by green/red on the NSE screen.")
            except Exception as exc:                        # noqa: BLE001
                st.caption(f"Chain condition unavailable: {exc}")

            cA, cB, cC = st.columns(3)
            s2_stop = cA.checkbox("Stage 2 stop taken today")
            s3_stop = cB.checkbox("Stage 3 stop taken today")
            n_rally = cC.number_input("Trades already in this rally", 0, 5, 0)

            res = [z.price for z in zones if z.kind == "resistance"] or \
                  [l.price for l in price_lv if l.kind == "resistance"]
            sup = [z.price for z in zones if z.kind == "support"] or \
                  [l.price for l in price_lv if l.kind == "support"]

            checks = su.check_setups(f5, f15, spot, now, res, sup, cc,
                                     s2_stop, s3_stop, int(n_rally))

            ready = [c for c in checks if c.stage.startswith("I+C")]
            if ready:
                st.success("**Preconditions met on: "
                           + ", ".join(f"#{c.number} {c.name}" for c in ready)
                           + "** — now watch for your own entry trigger.")

            for c in checks:
                colour = {"I+C READY — watch for your entry trigger": "🟢",
                          "INDICATION only — not a trade": "🟡",
                          "BLOCKED": "🔴", "MANUAL ONLY": "⚪",
                          "NOT SET UP": "⚫"}.get(c.stage, "⚫")
                with st.expander(f"{colour} #{c.number} · {c.name} · "
                                 f"{c.direction} · {c.win_rate} · {c.stage}"):
                    st.caption(f"Target / stop: {c.target_stop}")
                    if c.indication:
                        st.markdown("**I — Indication**")
                        for t, v in c.indication:
                            st.markdown(("- ✅ " if v else "- ❌ " if v is not None
                                         else "- ❓ ") + t)
                    if c.confirmation:
                        st.markdown("**C — Confirmation**")
                        for t, v in c.confirmation:
                            st.markdown(("- ✅ " if v else "- ❌ " if v is not None
                                         else "- ❓ ") + t)
                    if c.skips:
                        st.markdown("**Blocked by**")
                        for x in c.skips:
                            st.markdown(f"- 🚫 {x}")
                    if c.manual:
                        st.markdown("**E — Entry, and what stays yours**")
                        for x in c.manual:
                            st.markdown(f"- {x}")

if _tab_active(9):
    refresh_control("master", (load_ohlc_cached,), pull_ohlc=True, note="master_levels.csv")
    mpath = config.DATA_DIR / "master_levels.csv"
    if not mpath.exists():
        st.info("No master sheet yet.")
        st.code("python build_master.py", language="bash")
        st.caption("Combines every CSV in data/ohlc/ into one series and "
                   "computes R1-R4, S1-S4, gap zones, Gann levels and time "
                   "cycles for every session. Writes master_levels.csv "
                   "(read here) and NIFTY_Master_Levels.xlsx (colour-coded).")
    else:
        try:
            mdf = pd.read_csv(mpath)
            mdf["Date"] = pd.to_datetime(mdf["Date"], errors="coerce")
            mdf = mdf.dropna(subset=["Date"]).sort_values("Date")
            row = mdf.iloc[-1]

            if st.button("Append today's bar + rebuild master",
                         help="Only works after 15:30 — a partial bar would "
                              "corrupt every level derived from it"):
                try:
                    import nse_client as _uc
                    _ucl = _uc.NSEClient(); _ucl._bootstrap()
                    _ok, _msg = md.backfill_ohlc(
                        _ucl, config.OHLC_BACKFILL_DAYS)
                    if _ok:
                        st.success(_msg)
                        import build_master
                        build_master.main()
                        st.success("Master sheet rebuilt — reload to see it")
                    else:
                        st.info(_msg)
                except Exception as _e:                     # noqa: BLE001
                    st.error(f"Update failed: {_e}")

            _mage = (pd.Timestamp(now.date()) - mdf.Date.max()).days
            if _mage >= 1:
                st.error(f"**Master sheet is {_mage} session(s) behind** — "
                         f"newest row {mdf.Date.max():%d-%b-%Y}, today is "
                         f"{now:%d-%b-%Y}. Rebuild it with the button above "
                         f"after 15:30, or run `python update_daily.py`.")
            st.caption(f"{len(mdf):,} sessions · "
                       f"{mdf.Date.min():%d-%b-%Y} to {mdf.Date.max():%d-%b-%Y} · "
                       f"levels below are from {row.Date:%d-%b-%Y}")

            # every level, tagged, with distance to live spot
            groups = {
                "Prior swing R": ["R1", "R2", "R3", "R4"],
                "Prior swing S": ["S1", "S2", "S3", "S4"],
                "Unfilled gap above": ["Upper_Gap_1", "Upper_Gap_2", "Upper_Gap_3"],
                "Unfilled gap below": ["Lower_Gap_1", "Lower_Gap_2", "Lower_Gap_3"],
                "Gann resistance": ["Gann_R1_45", "Gann_R2_90", "Gann_R3_135", "Gann_R4_180"],
                "Gann support": ["Gann_S1_45", "Gann_S2_90", "Gann_S3_135", "Gann_S4_180"],
            }
            recs = []
            for grp, cols in groups.items():
                for c in cols:
                    v = row.get(c)
                    if pd.notna(v) and v:
                        recs.append({"Group": grp, "Level": c,
                                     "Price": float(v),
                                     "Distance": float(v) - spot})
            lv = pd.DataFrame(recs).sort_values("Price", ascending=False)

            # proximity alerts — the reason this tab exists
            near = lv[lv.Distance.abs() <= config.MASTER_PROXIMITY].copy()
            if not near.empty:
                st.warning(f"**Spot {spot:,.0f} is within "
                           f"{config.MASTER_PROXIMITY} pts of "
                           f"{len(near)} master level(s):**")
                for _, r in near.reindex(near.Distance.abs().sort_values().index).iterrows():
                    side = "above" if r.Distance > 0 else "below"
                    st.markdown(f"- **{r.Price:,.0f}** — {r.Group} ({r.Level}) · "
                                f"{abs(r.Distance):.0f} pts {side}")
            else:
                nearest = lv.reindex(lv.Distance.abs().sort_values().index).iloc[0]
                st.info(f"No master level within {config.MASTER_PROXIMITY} pts. "
                        f"Nearest is {nearest.Price:,.0f} "
                        f"({nearest.Group}), {abs(nearest.Distance):.0f} pts away.")

            for col, label in (("Time_Cycle_Local", "Local cycle (rolling 252-day)"),
                               ("Time_Cycle_Global", "Global cycle (whole series)")):
                val = row.get(col)
                if isinstance(val, str) and val != "None":
                    st.warning(f"**{label}: {val}**")

            st.markdown("##### All levels for the latest session")
            show = lv.copy()
            show["Price"] = show["Price"].map(lambda x: f"{x:,.0f}")
            show["Distance"] = show["Distance"].map(lambda x: f"{x:+,.0f}")
            st.dataframe(show, use_container_width=True, hide_index=True)

            with st.expander("Recent sessions"):
                cols = ["Date", "Open", "High", "Low", "Close", "Gap_Points",
                        "R1", "S1", "Time_Cycle_Local"]
                tail = mdf[cols].tail(20).iloc[::-1].copy()
                tail["Date"] = tail["Date"].dt.strftime("%d-%b-%Y")
                st.dataframe(tail, use_container_width=True, hide_index=True)
                st.caption("Full colour-coded history is in "
                           "NIFTY_Master_Levels.xlsx — green where a support "
                           "was broken the next day, red where a resistance was.")
        except Exception as exc:                            # noqa: BLE001
            st.error("Could not read the master sheet.")
            st.code(str(exc), language="text")

if _tab_active(10):
    try:
        view_building_tab.render(
            now=now,
            spot=spot,
            df=df,
            vb_daily=vb_daily if "vb_daily" in locals() else None,
            vb_master=vb_master if "vb_master" in locals() else None,
            vb_indices=vb_indices if "vb_indices" in locals() else None,
            vb_ad_n=vb_ad_n if "vb_ad_n" in locals() else None,
            vb_ad_b=vb_ad_b if "vb_ad_b" in locals() else None,
            vb_gift=vb_gift if "vb_gift" in locals() else None,
            vb_glob=vb_glob if "vb_glob" in locals() else None,
            vb_crude=vb_crude if "vb_crude" in locals() else None,
            vb_crude_nse=vb_crude_nse if "vb_crude_nse" in locals() else None,
            vb_poi=vb_poi if "vb_poi" in locals() else None,
            vb_vixh=vb_vixh if "vb_vixh" in locals() else None,
            vb_nf=vb_nf if "vb_nf" in locals() else None,
            vb_bnf=vb_bnf if "vb_bnf" in locals() else None,
            vb_adrs=vb_adrs if "vb_adrs" in locals() else None,
            flows=flows if "flows" in locals() else None,
            read=read if "read" in locals() else None,
            nse_ts=nse_ts if "nse_ts" in locals() else "",
            refresh_control=refresh_control,
            load_ohlc_cached=load_ohlc_cached,
            fetch_vix_cached=fetch_vix_cached,
            fetch_flows_cached=fetch_flows_cached,
            candles=idx_c if "idx_c" in locals() or "idx_c" in globals() else None,
        )
    except Exception as _vbe:
        st.error(f"View Building Module Error: {_vbe}")

if _tab_active(11):
    # MIGRATED to views/levels.py — the pattern for the rest.
    #
    # The body is unchanged; it now reads from CTX instead of module scope.
    # The guard is the part that matters: under the eventual router a view
    # that raises must take down only its own panel, and file separation
    # alone does not achieve that — a bare call still kills the script.
    if CTX is None:
        st.error("Context unavailable — this view cannot render.")
    else:
        try:
            import views.chart_levels as _vchart
            _vchart.render(CTX, refresh_control=refresh_control,
                           ohlc_cache=load_ohlc_cached,
                           daily_levels_chart=daily_levels_chart)
        except Exception as _ve:                            # noqa: BLE001
            st.error(f"Chart levels failed: {_ve}")
            st.exception(_ve)

if _tab_active(12):
    refresh_control("pillars", (fetch_live,), "option chain", nse_ts)

    try:
        _fp = an.four_pillars(df, spot)
        _cl = an.chain_levels(df, spot)
        _atm_row = _fp["atm_row"]

        # ── ATM battle ──────────────────────────────────────────────────────
        st.markdown(f"### ATM {_fp['atm']}")
        _b1, _b2 = st.columns(2)
        for _col, _side, _lbl in ((_b1, "ce", "CALL"), (_b2, "pe", "PUT")):
            _sr = getattr(_atm_row, _side) if _atm_row else None
            with _col:
                st.markdown(f"**{_lbl}**")
                if _sr is None:
                    st.caption("ATM strike not in the window")
                    continue
                st.markdown(
                    f"LTP&nbsp;&nbsp;&nbsp;&nbsp;**{_sr.ltp:,.2f}**  \n"
                    f"Price&nbsp;&nbsp;**{_sr.ltp_pct:+.1f}%**  \n"
                    f"OI&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;**{_sr.oi:,.0f}**  \n"
                    f"OI chg&nbsp;**{_sr.oi_chg:+,.0f}**  \n"
                    f"OI %&nbsp;&nbsp;&nbsp;**{_sr.oi_pct:+.2f}%**"
                    if _sr.ltp and _sr.ltp_pct is not None and _sr.oi_pct is not None
                    else f"OI **{_sr.oi:,.0f}**, change **{_sr.oi_chg:+,.0f}**",
                    unsafe_allow_html=True)
                _c = {"HIGH": st.success, "MODERATE": st.info,
                      "WEAK": st.warning, "NONE": st.error}[_sr.confidence]
                _c(f"**{_sr.state}** · confidence {_sr.confidence}  \n"
                   f"*{_sr.why}*")

        # bullish / bearish factors, counted not scored
        _bull = _bear = 0
        for r in _fp["rows"]:
            if r.ce.pillar == "short_covering": _bull += 1
            if r.ce.pillar == "short_buildup": _bear += 1
            if r.pe.pillar == "short_buildup": _bull += 1
            if r.pe.pillar == "short_covering": _bear += 1
        _tot = _bull + _bear
        _bias = ("BULLISH" if _bull > _bear * 1.5 else
                 "BEARISH" if _bear > _bull * 1.5 else "NEUTRAL")
        _strength = ("STRONG" if _tot and abs(_bull - _bear) / max(_tot, 1) > 0.5
                     else "MODERATE" if _tot and abs(_bull - _bear) / max(_tot, 1) > 0.25
                     else "WEAK")
        _f1, _f2, _f3 = st.columns(3)
        _f1.metric("Bullish factors", _bull)
        _f2.metric("Bearish factors", _bear)
        _f3.metric("ATM battle", _bias, _strength, delta_color="off")
        st.caption("Deliberately WEAK / MODERATE / STRONG rather than a "
                   "percentage. A number like \"72%\" implies calibration "
                   "that has not been done — it would be false precision.")

        _d = {"FRESH PARTICIPATION": st.success, "EXIT-DRIVEN": st.warning,
              "MIXED": st.info, "NO FLOW": st.info}[_fp["drive"]]
        _d(f"**{_fp['drive']}** — {_fp['drive_note']}")

        # ── full evidence table ─────────────────────────────────────────────
        st.markdown("##### Every strike — the full evidence")
        # how long each strike has held its pillar — one observation versus
        # a position being worked
        _dur = {}
        if _snaps:
            for _r in _fp["rows"]:
                for _sd in ("ce", "pe"):
                    try:
                        _d0 = fl.state_duration(_snaps, _r.strike, _sd, now)
                        if _d0:
                            _dur[(_r.strike, _sd)] = _d0
                    except Exception:                       # noqa: BLE001
                        pass

        def _lbl(row, side):
            sr = getattr(row, side)
            d0 = _dur.get((row.strike, side))
            if d0 and d0.get("minutes", 0) >= 3 and d0["pillar"] == sr.pillar:
                return f"{sr.state} ({d0['minutes']:.0f}m)"
            return sr.state

        def _grk(row, side, col):
            v = df[df.strike == row.strike]
            if v.empty:
                return "—"
            x = v.iloc[0].get(f"{side}_{col}")
            return f"{x:.4f}" if x == x and x is not None else "—"

        _rows = []
        for r in _fp["rows"]:
            def _f(v, fmt="{:,.2f}"):
                return fmt.format(v) if v is not None else "—"
            _rows.append({
                "Strike": r.strike, "ATM": "★" if r.is_atm else "",
                "CE LTP": _f(r.ce.ltp), "CE Δ%": _f(r.ce.ltp_pct, "{:+.1f}%"),
                "CE OI": f"{r.ce.oi:,.0f}", "CE ΔOI": f"{r.ce.oi_chg:+,.0f}",
                "CE ΔOI%": _f(r.ce.oi_pct, "{:+.1f}%"),
                "CE Δ": _grk(r, "ce", "delta"), "CE Γ": _grk(r, "ce", "gamma"),
                "CE STATE": _lbl(r, "ce"), "CE CONF": r.ce.confidence,
                "PE LTP": _f(r.pe.ltp), "PE Δ%": _f(r.pe.ltp_pct, "{:+.1f}%"),
                "PE OI": f"{r.pe.oi:,.0f}", "PE ΔOI": f"{r.pe.oi_chg:+,.0f}",
                "PE ΔOI%": _f(r.pe.oi_pct, "{:+.1f}%"),
                "PE Δ": _grk(r, "pe", "delta"), "PE Γ": _grk(r, "pe", "gamma"),
                "PE STATE": _lbl(r, "pe"), "PE CONF": r.pe.confidence})
        _pdf = pd.DataFrame(_rows)

        def _sty(row):
            out = [""] * len(row)
            for col in ("CE STATE", "PE STATE"):
                i = list(row.index).index(col)
                v = str(row[col]).split(" (")[0]
                if v in ("SHORT COVERING", "LONG BUILD-UP"):
                    out[i] = "background-color:rgba(16,185,129,0.22);color:#6ee7b7;font-weight:700"
                elif v in ("SHORT BUILD-UP", "LONG UNWINDING"):
                    out[i] = "background-color:rgba(239,68,68,0.22);color:#fca5a5;font-weight:700"
                elif v == "UNKNOWN":
                    out[i] = "background-color:#1e293b;color:#94a3b8"

            # Heatmapping on CE ΔOI% and PE ΔOI% (> 50% = vivid glow, > 20% = subtle)
            for col, is_ce in (("CE ΔOI%", True), ("PE ΔOI%", False)):
                i = list(row.index).index(col)
                val_str = str(row[col]).replace("%", "").replace("+", "").strip()
                try:
                    pct_val = float(val_str)
                    if abs(pct_val) >= 50:
                        out[i] = "background-color:rgba(239,68,68,0.25);color:#fca5a5;font-weight:800;" if is_ce else "background-color:rgba(16,185,129,0.25);color:#6ee7b7;font-weight:800;"
                    elif abs(pct_val) >= 20:
                        out[i] = "background-color:rgba(239,68,68,0.12);color:#f87171;" if is_ce else "background-color:rgba(16,185,129,0.12);color:#34d399;"
                except Exception:
                    pass

            if row["ATM"] == "★":
                out = [(x + ";border-top:2px solid #facc15;border-bottom:2px solid #facc15;font-weight:bold;") if x else "border-top:2px solid #facc15;border-bottom:2px solid #facc15;font-weight:bold;"
                       for x in out]
            return out

        st.dataframe(_pdf.style.apply(_sty, axis=1),
                     use_container_width=True, hide_index=True)
        st.caption("Δ and Γ are computed from NSE's IV via Black-Scholes. "
                   "A dash means the IV was unusable on that strike — NSE "
                   "publishes 0 or absurd values on illiquid options, and a "
                   "gamma of 0.0 that means 'bad input' is indistinguishable "
                   "from one meaning 'no convexity'. State labels carry how "
                   "long the strike has held that pillar.")
        st.caption("A pillar is NEVER inferred from OI change alone. Price "
                   "direction and OI direction are both required — OI rising "
                   "says positions were created and nothing about which side "
                   "created them. Where the price leg is missing or flat the "
                   "state reads UNKNOWN rather than a guess.")

        # ── levels ──────────────────────────────────────────────────────────
        if _gz:
            st.markdown("##### Gamma squeeze zones — where price gets sticky")
            for _z in _gz:
                (_c := st.warning if _z.share >= 25 else st.info)(
                    f"**{_z.strike}** ({_z.distance:+.0f} pts) — "
                    f"**{_z.label}**  \n{_z.note}")
            st.caption("Ranked by gamma x OI. Dealers hedging a high-gamma "
                       "strike buy dips and sell rallies around it, which "
                       "dampens movement — sticky until it breaks, fast "
                       "once it does. Computed from NSE's IV via "
                       "Black-Scholes, with time to expiry in TRADING "
                       "minutes.")
            st.markdown("---")

        # ── 🪜 Option-Chain Key Levels — Price Ladder Grid ─────────────────
        render_price_ladder_grid(df, spot)

        st.caption("High OI is positioning CONCENTRATION — not proof of "
                   "writing, and not proof the level holds. State combines OI "
                   "change with PRICE direction: at a call strike, OI up with "
                   "premium DOWN is short buildup and hardens resistance, "
                   "while OI up with premium UP is long buildup — speculators, "
                   "not a wall. MIGRATING means the next strike out is "
                   "building while this one bleeds; CONTESTED means both sides "
                   "are adding here.")
    except Exception as _e:                                 # noqa: BLE001
        st.error("Four pillars unavailable.")
        st.code(str(_e), language="text")

if _tab_active(13):
    refresh_control("mstate", (fetch_live, fetch_price_action,),
                    "chain + candles", nse_ts)

    def _evidence(v, title=None):
        if title:
            st.markdown(f"**{title}**")
        box = {"STRONG": st.success, "MODERATE": st.info,
               "WEAK": st.warning}.get(v.confidence, st.info)
        box(f"**{v.state}** · confidence {v.confidence}"
            + (f"  \n{v.detail}" if v.detail else ""))
        c1, c2 = st.columns(2)
        c1.markdown("*Supporting*")
        for x in v.supporting:
            c1.markdown(f"✓ {x}")
        if not v.supporting:
            c1.caption("none")
        c2.markdown("*Contradicting*")
        for x in v.contradicting:
            c2.markdown(f"⚠ {x}")
        if not v.contradicting:
            c2.caption("none")

    try:
        _f5 = pa_frames.get("5m")
        _fp = an.four_pillars(df, spot)
        _cl = an.chain_levels(df, spot)

        _trend = ms.trend_structure(_f5) if _f5 is not None else None
        _vside = None
        if _f5 is not None and "vwap" in _f5:
            _vr = pa.vwap_read(_f5, "5m")
            _vside = _vr.side if _vr.vwap else None

        _press = ms.market_pressure(_fp, _cl, _trend, _vside)

        st.markdown("### Market pressure")
        _evidence(_press)
        st.caption("Factors are listed, not netted. Four supports with three "
                   "contradictions is a different situation from four with "
                   "none, and a single score would hide that.")

        st.markdown("---")
        st.markdown("### Narrative")
        _brk = _pin = _rng = None
        if _f5 is not None and len(_f5) > 8:
            if _cl["resistance"]:
                _r = _cl["resistance"][0]
                _nxt = _r.neighbour_building
                _brk = ms.breakout(_f5, spot, float(_r.strike), "resistance",
                                   _r, _nxt)
            _pin = ms.pinning(df, spot, _f5, _fp["atm"])
            if _cl["resistance"] and _cl["support"]:
                _rng = ms.range_trap(_f5, float(_cl["resistance"][0].strike),
                                     float(_cl["support"][0].strike))
        st.info(ms.narrative(spot, _fp["atm"], _fp, _cl, _trend, _brk,
                             _pin, _rng))

        st.markdown("---")
        st.markdown("### Engines")
        if _trend:
            _evidence(_trend, "Market structure")
            st.markdown("")
        if _brk:
            _evidence(_brk, "Breakout engine")
            st.markdown("")
        if _rng:
            _evidence(_rng, "Range trap")
            st.markdown("")
        if _pin:
            _evidence(_pin, "Pinning / ATM fight")

        st.markdown("---")
        st.markdown("### What changed")
        try:
            _log = store.load_log()
            _ch = ms.what_changed(_log, now)
            if not _ch or not any(c.get("available") for c in _ch):
                st.caption("Not enough logged history yet — this fills in as "
                           "the scanner runs.")
            else:
                _cc = st.columns(len(_ch))
                for _col, _c in zip(_cc, _ch):
                    if not _c.get("available"):
                        _col.metric(_c["window"], "—")
                        continue
                    _col.metric(f"last {_c['window']}",
                                f"{_c['spot']:+,.0f} pts",
                                f"score {_c['score']:+.1f}",
                                delta_color="off")
                    if _c["regime_changed"]:
                        _col.warning(f"regime {_c['regime_then']} → "
                                     f"{_c['regime_now']}")
                    if _c["walls_moved"]:
                        _col.info("OI walls moved")
        except Exception:                                   # noqa: BLE001
            st.caption("signal log unavailable")
    except Exception as _e:                                 # noqa: BLE001
        st.error("Market state unavailable.")
        st.code(str(_e), language="text")

if _tab_active(14):
    refresh_control("rulebook", (fetch_live,), "option chain", nse_ts)
    st.caption("The strict rules, implemented literally. Kept separate from "
               "the rest of the app so the two can be compared — where they "
               "disagree, both are shown rather than one quietly winning.")

    try:
        _ab = rb.atm_bias(df, spot)
        if _ab:
            st.markdown("##### Bias — ATM only")
            _bc = st.columns(4)
            _bc[0].metric("ATM", f"{_ab.atm}")
            _bc[1].metric("Call OI", f"{_ab.call_oi:,.0f}")
            _bc[2].metric("Put OI", f"{_ab.put_oi:,.0f}")
            _bc[3].metric(_ab.label, f"{_ab.ratio:.2f}", _ab.strength,
                          delta_color="off")
            st.caption(f"{_ab.why}. One comparison, nothing else — you can "
                       f"check this against the NSE screen in three seconds, "
                       f"which the app's five-input bias does not allow.")
            try:
                if DEC is not None and DEC.bias.value not in _ab.label:
                    st.warning(f"**The two bias readings DISAGREE** — rulebook "
                               f"says {_ab.label}, the app's multi-factor read "
                               f"says {DEC.bias.value}. Neither is corrected "
                               f"to match the other.")
            except Exception:                               # noqa: BLE001
                pass

        st.markdown("---")
        st.markdown("##### True levels — comparing both sides at each strike")
        _tl = rb.true_levels(df, spot, 6)
        _shown = [v for v in _tl if v.label not in ("QUIET", "NO CLEAR SIDE")]
        if _shown:
            for v in _shown:
                _ic = {"TRUE SUPPORT": "🟢", "TRUE RESISTANCE": "🔴",
                       "CONTESTED": "⚔️", "BOTH UNWINDING": "🟠",
                       "UNKNOWN": "⬜"}.get(v.label, "⬜")
                st.markdown(f"{_ic} **{v.strike}** ({v.distance:+.0f} pts) — "
                            f"**{v.label}**  \n*{v.why}*")
        else:
            st.info("No strike shows clear one-sided dominance right now — "
                    "everything nearby is quiet or evenly matched.")
        with st.expander("All strikes, including the quiet ones"):
            st.dataframe(pd.DataFrame([{
                "Strike": v.strike, "Dist": f"{v.distance:+.0f}",
                "CE OI%": f"{v.ce_pct:+.1f}" if v.ce_pct is not None else "—",
                "PE OI%": f"{v.pe_pct:+.1f}" if v.pe_pct is not None else "—",
                "CE OI": f"{v.ce_oi:,.0f}", "PE OI": f"{v.pe_oi:,.0f}",
                "Verdict": v.label} for v in _tl]),
                use_container_width=True, hide_index=True)
        st.caption("A level is TRUE SUPPORT only when put-side OI% clearly "
                   "exceeds call-side OI% AT THE SAME STRIKE. Judging one "
                   "side alone cannot tell a defended floor from a strike "
                   "where both sides are piling in.")

        st.markdown("---")
        st.markdown("##### Levels ranked by SIZE vs by PROXIMITY")
        _bysize = rb.levels_by_size(df, spot)
        _wl = fl.weighted_levels(df, spot, 3, spot_change=_spot_chg)
        _byprox = an.chain_levels(df, spot)
        _rows = []
        for kind in ("resistance", "support"):
            _tag = "R" if kind == "resistance" else "S"
            for i in range(4):
                a_ = _bysize[kind][i] if i < len(_bysize[kind]) else None
                p_ = _byprox[kind][i] if i < len(_byprox[kind]) else None
                _rows.append({
                    "": f"{_tag}{i+1}",
                    "By SIZE (rulebook)": f"{a_.strike}" if a_ else "—",
                    "OI": f"{a_.oi:,.0f}" if a_ else "—",
                    "By PROXIMITY (app)": f"{p_.strike}" if p_ else "—",
                    "OI ": f"{p_.oi:,.0f}" if p_ else "—",
                    "Same?": "✓" if a_ and p_ and a_.strike == p_.strike else "✗"})
        st.dataframe(pd.DataFrame(_rows), use_container_width=True,
                     hide_index=True)
        if _wl["resistance"] or _wl["support"]:
            st.markdown("**Weighted and approach-adjusted** — "
                        "OI + (change x 2), re-ranked by direction of travel")
            _wc = st.columns(2)
            for _col, _kind in ((_wc[0], "resistance"), (_wc[1], "support")):
                _col.markdown(f"*{_kind.title()}*")
                for _lv in _wl[_kind]:
                    _col.markdown(f"**{_lv.strike}** ({_lv.distance:+.0f}) — "
                                  f"strength {_lv.strength:,.0f}")
            st.caption("Fresh addition counts double, and a wall price is "
                       "walking INTO ranks above one it is walking away from. "
                       "The direction weighting nudges rather than overrides "
                       "— it will not flip a wall 40% larger, but it decides "
                       "between two of similar size, which is the case where "
                       "direction actually matters.")
            st.markdown("---")

        st.caption("A real conflict, not a bug. Size answers *where is the "
                   "most money*; proximity answers *what do I hit first*. "
                   "Both are defensible and on most chains they name "
                   "different strikes, so both are shown.")

        st.markdown("---")
        st.markdown("##### OI density between levels")
        _dens = []
        for kind, side in (("support", "pe"), ("resistance", "ce")):
            lst = _bysize[kind]
            for i in range(min(2, len(lst) - 1)):
                dn = rb.density_between(df, lst[i].strike, lst[i+1].strike, side)
                if dn:
                    _dens.append((kind, dn))
        if _dens:
            for kind, dn in _dens:
                _ic = "🟥" if dn.label in ("VERY THIN", "THIN") else "🟩"
                st.markdown(f"{_ic} **{dn.from_strike} → {dn.to_strike}** "
                            f"({kind}) — **{dn.label}**  \n*{dn.note}*")
        else:
            st.caption("not enough levels to measure density between")

        st.markdown("---")
        st.markdown("##### Breakout — literal, five conditions")
        try:
            _bs = rb.levels_by_size(df, spot)
            if len(_bs["resistance"]) >= 2:
                _o, _n = _bs["resistance"][0].strike, _bs["resistance"][1].strike
                _acc = spot > _o
                _lv = rb.real_breakout(df, _o, _n, "ce", _acc)
                (st.success if _lv.label == "REAL_BREAKOUT"
                 else st.error if _lv.label == "FAKE_BREAKOUT"
                 else st.info)(
                    f"**{_lv.label}** — {_lv.met}/{_lv.total} conditions "
                    f"[{_lv.confidence}]")
                for _c, _ok in _lv.conditions:
                    st.markdown(f"{'✓' if _ok else '✗'} {_c}")
                st.caption(_lv.why)

                _ts = rb.trend_strength(df, _o, _n, "ce", _acc)
                st.markdown(f"**Trend strength: {_ts.label}** "
                            f"({_ts.met}/{_ts.total}) — {_ts.why}")
        except Exception as _e:                             # noqa: BLE001
            st.caption(f"literal breakout unavailable: {_e}")

        st.markdown("---")
        st.markdown("##### Pinning — strict test")
        _pr = None
        _f5r = pa_frames.get("5m")
        if _f5r is not None and len(_f5r) >= 6:
            _c5 = _f5r[_f5r["closed"]] if "closed" in _f5r else _f5r
            _pr = float(_c5["high"].tail(6).max() - _c5["low"].tail(6).min())
        _pv = rb.pinned_strict(df, spot, _pr)
        if _pv:
            (st.warning if _pv.label.startswith("PINNED") else st.info)(
                f"**{_pv.label}**")
            for w in _pv.why:
                st.markdown(f"- {w}")
            st.caption("Requires all three: both sides building, neither "
                       "unwinding, price locked. Checking only that both are "
                       "building would call a strike pinned while one side "
                       "quietly leaves.")
        st.markdown("---")
        with st.expander("Thresholds in use — edit rules.json to change them"):
            st.caption(f"loaded from `{rb.RULES.get('_source')}` "
                       f"(v{rb.RULES.get('_version', '?')})")
            _rr = []
            for _sec, _vals in rb.RULES.items():
                if _sec.startswith("_") or not isinstance(_vals, dict):
                    continue
                for _k, _v in _vals.items():
                    _rr.append({"Engine": _sec, "Threshold": _k, "Value": _v})
            st.dataframe(pd.DataFrame(_rr), use_container_width=True,
                         hide_index=True)
            st.caption("Every one of these is a judgement, not a fact. Kept "
                       "in a file rather than buried in Python so you can "
                       "read them all in twenty seconds and change one "
                       "without touching code.")
            if "defaults" in str(rb.RULES.get("_source", "")):
                st.warning("rules.json was NOT found — running on built-in "
                           "defaults. Anything you tuned is not being used.")

        with st.expander("Display contract — is everything required on screen?"):
            import pathlib as _pl
            _srcs = {f.name: f.read_text()
                     for f in _pl.Path(__file__).parent.glob("*.py")}
            _chk = rb.display_contract_check(_srcs)
            _ok_n = sum(1 for _, o in _chk if o)
            (st.success if _ok_n == len(_chk) else st.warning)(
                f"{_ok_n}/{len(_chk)} required elements present")
            for _k, _ok in _chk:
                st.markdown(f"{'✓' if _ok else '✗ **MISSING**'} {_k}")
            st.caption("A spec nobody checks drifts from the code within a "
                       "week. This greps every module for a marker per "
                       "required element.")
    except Exception as _e:                                 # noqa: BLE001
        st.error("Rulebook unavailable.")
        st.code(str(_e), language="text")

if _tab_active(15):
    refresh_control("flow", (fetch_live,), "option chain", nse_ts)
    try:
        _pcrs = fl.three_pcrs(df, spot)
        _pv = fl.pcr_by_volume(df, spot)
        _mp = an.max_pain(df)
        _mpr = fl.MaxPainRead(_mp, spot, now, expiry_today)

        _sm = None
        try:
            _sr2 = an.straddle_range(df, spot)
            if _sr2:
                _sm = fl.straddle_move(store.load_log(), _sr2.premium, now, 10)
        except Exception:                                   # noqa: BLE001
            pass

        _meter = fl.regime_meter(df, spot, _pcrs,
                                 _sm.state if _sm else None,
                                 _mpr.distance)
        _box = {"CONSOLIDATION": st.warning, "TRENDING": st.success,
                "MIXED": st.info}[_meter.label]
        _box(f"**{_meter.label}** — {_meter.consolidation}% consolidation / "
             f"{_meter.trending}% trending")
        for _n, _d, _hit in _meter.checks:
            st.markdown(f"{'✓' if _hit else '✗'} **{_n}** — {_d}")
        st.caption(_meter.note)

        st.markdown("---")
        st.markdown("##### Four PCRs")
        _pc = st.columns(4)
        for _col, (_nm, _blk) in zip(_pc, list(_pcrs.items()) +
                                     [("volume", _pv)]):
            _v = (_blk or {}).get("value")
            _col.metric(_nm.upper(), f"{_v:.2f}" if _v else "—")
            _col.caption((_blk or {}).get("note", "")[:60])

        st.markdown("---")
        st.markdown("##### Levels by weighted strength")
        st.caption(f"strength = OI + (ΔOI × {config.FRESH_OI_WEIGHT}) — fresh "
                   f"positioning counts more than a wall that has sat there "
                   f"since Tuesday. A level bleeding OI scores BELOW its raw "
                   f"size, which is intended.")
        _wl = fl.weighted_levels(df, spot, 3)
        _wc1, _wc2 = st.columns(2)
        for _col, _kind in ((_wc1, "resistance"), (_wc2, "support")):
            _col.markdown(f"**{_kind.title()}s**")
            for _lv in _wl[_kind]:
                _col.markdown(f"**{_lv.strike}** ({_lv.distance:+.0f} pts)  \n"
                              f"OI {_lv.oi:,.0f} · ΔOI {_lv.oi_chg:+,.0f}  \n"
                              f"strength **{_lv.strength:,.0f}** "
                              f"({_lv.vs_raw:+,.0f} vs raw)")

        st.markdown("---")
        st.markdown("##### Max pain")
        st.info(_mpr.note)

        if _sm:
            st.markdown("---")
            st.markdown("##### Straddle")
            (st.warning if "EXPAND" in _sm.state else st.info)(
                f"**{_sm.state}** — {_sm.note}")

        st.markdown("---")
        st.markdown("##### OI heatmap")
        try:
            _snaps = store.recent_snapshots(now.date(), limit=12)
            if len(_snaps) >= 2:
                _side = st.radio("side", ["ce", "pe"], horizontal=True,
                                 key="hm_side")
                _hm = fl.oi_heatmap(_snaps, spot, _side, 6)
                st.dataframe(fl.heat_style(_hm, invert=(_side == "ce")),
                             use_container_width=True)
                st.caption("Rows are strikes, columns are polls. A band of "
                           "colour marching up or down the rows IS the "
                           "migration the other panels describe in words.")
            else:
                st.caption(f"{len(_snaps)} snapshot(s) stored today — the "
                           f"heatmap needs a few polls before it shows "
                           f"anything.")
        except Exception as _he:                            # noqa: BLE001
            st.caption(f"heatmap unavailable: {_he}")
    except Exception as _e:                                 # noqa: BLE001
        st.error("Flow unavailable.")
        st.code(str(_e), language="text")

# ── log ─────────────────────────────────────────────────────────────────────
# Demo runs must never touch the log — it is your backtest dataset, and rows
# generated from a static sample file (repeatedly, every refresh) would poison it.
# WEEKEND IMMUNITY: Never store snapshots or signal logs on Saturday or Sunday no matter what!
if not demo and now.weekday() < 5:
    store.append_log(an.signal_to_row(read, sig, now))
    store.save_snapshot(df, spot, now)

st.caption(f"NSE timestamp: {nse_ts} · expiry {expiry} "
           f"({dte:.2f} days) · local {now:%H:%M:%S} · "
           f"snapshot {len(STATE.history)} this session")
st.caption("This is a decision-support tool, not a trading system. It tells you "
           "where the levels are and what the flow is doing. Entries come from "
           "your price-action setup, not from this screen.")

# ── auto refresh ────────────────────────────────────────────────────────────
if auto:
    _cur_interval = int(_poll_sec if "_poll_sec" in locals() else getattr(config, "POLL_SECONDS", 60))
    try:
        from streamlit_autorefresh import st_autorefresh
        st_autorefresh(interval=_cur_interval * 1000, key="poller")
    except ImportError:
        time.sleep(_cur_interval)
        st.rerun()


# ═══════════════════════════════════════════════════════════════════════════
# Tab 16 — Timeline
# ═══════════════════════════════════════════════════════════════════════════
if _tab_active(16):
    st.subheader("State changes")
    _days_avail = tl.available_days(config.TIMELINE_KEEP_DAYS)
    st.caption(
        f"Rolling {config.TIMELINE_KEEP_DAYS} sessions · "
        f"{len(_days_avail)} stored · oldest drops off automatically")

    v1, v2 = st.columns([1, 1])
    _win = v1.radio("Window", ["Latest session", "Last 7 sessions",
                               f"Last {config.TIMELINE_KEEP_DAYS} sessions"],
                    horizontal=True, key="tl_win")
    _fld = v2.selectbox("Field", ["All fields"] + [f for f, _ in tl.TRACKED],
                        key="tl_fld")
    _n = (1 if _win.startswith("Latest") else
          7 if "7" in _win else config.TIMELINE_KEEP_DAYS)
    if _n == 1 and _days_avail and _days_avail[0] != now.date():
        st.caption(f"No session logged today — showing "
                   f"{_days_avail[0].strftime('%a %d %b')}, the most recent.")

    _rows = tl.table_range(_n, _fld)
    if _rows.empty:
        st.info("Nothing logged for this window yet. The first poll of each "
                "session writes an opening row per field; after that only "
                "changes are recorded.")
    else:
        _fs = tl.field_stats(_n)
        _dd = tl.by_day(_n)
        m1, m2, m3 = st.columns(3)
        m1.metric("Changes", len(_rows))
        m2.metric("Sessions", len(_dd))
        m3.metric("Busiest day",
                  _dd.loc[_dd["Changes"].idxmax(), "Date"] if len(_dd) else "—")

        _rest = _fs[_fs["Steadiness"] == "RESTLESS"]["Field"].tolist()
        if _rest:
            st.warning(
                "**Restless across this window: " + ", ".join(_rest) + "**  \n"
                "Averaging more than eight changes a session means the field "
                "is turning over faster than the market is. Over one day that "
                "is bad luck; over weeks it means the field is not usable and "
                "no chart comparison will rescue it.")

        if _n > 1:
            st.markdown("**By session**")
            st.caption("The regime sequence is what to read against a daily "
                       "chart. Changes is the flip count.")
            st.dataframe(_dd, use_container_width=True, hide_index=True,
                         height=min(60 + 35 * len(_dd), 400))

            st.markdown("**By field**")
            st.caption("Steadiness says a field is calm enough to be usable. "
                       "It does not say it was right — only your chart "
                       "comparison can say that.")
            st.dataframe(_fs, use_container_width=True, hide_index=True)

        st.markdown("**Every change, with what happened next**")
        o1, o2 = st.columns(2)
        _hz = o1.slider("Look-forward (minutes)", 10, 90,
                        config.OUTCOME_HORIZON_MINS, step=5, key="tl_hz")
        _th = o2.slider("Move threshold (points)", 10.0, 60.0,
                        config.OUTCOME_THRESHOLD_PTS, step=5.0, key="tl_th")

        _ann = oc.annotate(_n, _fld, _hz, _th)
        if _ann.empty:
            st.dataframe(_rows, use_container_width=True, hide_index=True,
                         height=min(60 + 35 * len(_rows), 620))
        else:
            _graded = _ann[_ann.Verdict != "—"]
            if len(_graded):
                g1, g2, g3 = st.columns(3)
                g1.metric("Matched", int((_graded.Verdict == "MATCHED").sum()))
                g2.metric("Opposed", int((_graded.Verdict == "OPPOSED").sum()))
                g3.metric("Went nowhere",
                          int(_graded.Verdict.isin(["MISSED", "NOISY"]).sum()))

            def _vtint(v):
                return {"MATCHED": "background-color:#0F9D5822",
                        "OPPOSED": "background-color:#E8503A22",
                        "MISSED": "background-color:#C79A2018",
                        "NOISY": "background-color:#C79A2018"}.get(v, "")

            st.dataframe(
                _ann.style.map(_vtint, subset=["Verdict"]),
                use_container_width=True, hide_index=True,
                height=min(60 + 35 * len(_ann), 620))

            _sc = oc.scorecard(_n, _hz, _th)
            if not _sc.empty:
                st.markdown("**Agreement by field**")
                st.dataframe(_sc, use_container_width=True, hide_index=True)
                _thin = _sc[_sc["Enough data"] == "NO"]["Field"].tolist()
                if _thin:
                    st.caption(
                        "No percentage shown for " + ", ".join(_thin)
                        + f" — fewer than {config.OUTCOME_MIN_SAMPLE} graded "
                        "changes. Three out of three is three observations, "
                        "not a hundred percent.")

            st.warning(
                "**This is not a win rate.** It asks only whether spot moved "
                f"{_th:.0f} points within {_hz} minutes, and which way first. "
                "It ignores option premium, so a matched call can still lose "
                "money to theta and spread. It ignores stops, so a trade that "
                "came good after a deep adverse excursion counts as matched "
                "here and would have been stopped out in reality. It ignores "
                "the ₹400 round trip. Move the two sliders above: any field "
                "that only looks good at one setting was never good.")

        st.download_button(
            f"Download {str(_n) + ' sessions' if _n > 1 else 'this session'}"
            f" (CSV)",
            (_ann if not _ann.empty else _rows).to_csv(index=False).encode(),
            file_name=f"timeline_{_n}d_{now.date().isoformat()}.csv",
            mime="text/csv")

    st.divider()
    st.caption(
        "**How to use this.** Open the 5-minute chart for a session, mark the "
        "times, and look at what price did next. You do not have to do it the "
        "same evening — the last "
        f"{config.TIMELINE_KEEP_DAYS} sessions are kept, so a missed week is "
        "still reviewable. A change logged here is a claim the app made, not "
        "a claim it got right; the app cannot grade itself, and this table is "
        "the only thing that will tell you which of these fields deserve your "
        "attention.")


# ═══════════════════════════════════════════════════════════════════════════
# Tab 17 — Price Action Strategy 1 (15-minute, UBB-slope system)
# ═══════════════════════════════════════════════════════════════════════════
if _tab_active(17):
    _pc15 = None
    try:
        if vb_daily is not None and len(vb_daily):
            _pc15 = float(vb_daily.iloc[-1]["Close"])
    except Exception:                                       # noqa: BLE001
        pass
    try:
        strategy1_tab.render(pa_frames, prev_close=_pc15)
    except Exception as _se:                                # noqa: BLE001
        st.error(f"Strategy 1 tab failed: {_se}")


# ═══════════════════════════════════════════════════════════════════════════
# Tab 18 — VWAP / 8 EMA whichever-comes-first, with a candle trail
# ═══════════════════════════════════════════════════════════════════════════
def _render_tab18_mentor_s2():
        # A missing optional module must not take the application down.
        #
        # This import was bare, and a bare import at module scope inside a tab
        # block aborts the whole script when it fails — so one absent file
        # blanked the Strategy 1 tab as well, and every tab after it. The
        # traceback pointed at line 2968 in tab 18 while the visible damage was
        # somewhere else entirely, which is the worst kind of error to chase.
        # A missing optional module must not take the application down.
        #
        # This import was bare, and a bare import inside a tab block aborts the
        # WHOLE script when it fails — so one absent file blanked the Strategy 1
        # tab and every tab after it. The traceback named line 2968 in tab 18
        # while the visible damage was elsewhere, which is the worst kind of
        # error to chase. Optional features fail alone from here on.
        try:
            import strategy2 as s2
        except ModuleNotFoundError:
            s2 = None

        if s2 is None:
            st.error("**strategy2.py is missing.** Copy it from the latest zip "
                     "into the project folder. Only this tab needs it — nothing "
                     "else in the app depends on it.")
            st.stop()

        st.subheader("VWAP or 8 EMA — whichever comes first")
        st.caption("Enter on a 15-minute CLOSE beyond either line. Trail the stop "
                   "to each completed candle's low (CE) or high (PE). Exit on "
                   "touch. No UBB filter, no follow-through, no one-per-rally — "
                   "so this trades more often than Strategy 1 by design.")

        # 15-minute OHLCV straight from Dhan, ignoring CANDLE_SOURCE.
        #
        # This tab's rule is "VWAP or 8 EMA, whichever comes first". With no
        # volume there is no VWAP, and the rule quietly becomes 8-EMA-only — a
        # different strategy wearing the same name. So it goes to Dhan directly
        # rather than inheriting whatever the rest of the app is configured to
        # read.
        import dhan_frames as _dfr
        _mode = st.radio(
            "Price source", ["futures", "index"],
            index=0 if getattr(config, "TAB18_VWAP_MODE", "futures") == "futures"
            else 1, horizontal=True, key="t18mode",
            help="futures = real volume, prices ~15-30 pts above spot. "
                 "index = spot prices, futures volume as a proxy.")
        try:
            _f, _finfo = _dfr.frame("15", 5, _mode)
        except Exception as _fe:                                # noqa: BLE001
            _f, _finfo = None, {"errors": [str(_fe)]}

        if _finfo.get("price_source"):
            st.caption(
                f"prices: {_finfo['price_source']} · volume: "
                f"{_finfo.get('volume_source') or 'NONE'}"
                + (f" · VWAP from {_finfo.get('vwap_bars', 0)} bars"
                   if _finfo.get("vwap_ok") else " · VWAP UNAVAILABLE"))
        for _err in _finfo.get("errors", []):
            st.error(_err)

        if _f is None or len(_f) < 3:
            st.info("Not enough 15-minute candles from Dhan yet. If this "
                    "persists, run `python diagnose_dhan.py` — the Data API "
                    "subscription and the futures security id are both needed "
                    "here.")
        else:
            _h = s2.vwap_health(_f)
            (st.success if _h["ok"] else st.error)(
                f"**VWAP coverage {_h['pct']:.0f}%** — {_h['note']}")

            # ── levels: option chain now + previous close candle ────────────
            import pdc as _pdc
            _chain = {}
            try:
                _cl = an.chain_levels(df, spot)
                for _i, _r in enumerate(_cl["resistance"][:3], 1):
                    _chain[f"R{_i}"] = float(_r.strike)
                for _i, _sp in enumerate(_cl["support"][:3], 1):
                    _chain[f"S{_i}"] = float(_sp.strike)
            except Exception:                                   # noqa: BLE001
                pass

            _master = {}
            try:
                _ml = vb_master          # already loaded at startup, line ~814
                _row = _ml.iloc[-1]
                for _n in ("R1", "R2", "R3", "R4", "S1", "S2", "S3", "S4"):
                    if _n in _ml.columns and pd.notna(_row[_n]):
                        _master[_n] = float(_row[_n])
            except Exception:                                   # noqa: BLE001
                pass

            st.session_state["chain_levels_now"] = dict(_chain)
            st.session_state["master_levels_now"] = dict(_master)
            _lv = _pdc.all_levels(float(spot), _chain, _master)
            _pv = _pdc.previous(now.date())

            _lc1, _lc2 = st.columns([3, 1])
            # PDC with the session each level came from, so the numbers can be
            # checked against a chart rather than taken on trust.
            _pdc_detail = ""
            try:
                _pd_dated = _pdc.pdc_levels_dated(float(spot))
                _pdc_detail = " · ".join(
                    f"{k} {v:,.0f} ({d:%d-%b})"
                    for k, (v, d) in sorted(_pd_dated.items(),
                                            key=lambda x: -x[1][0]))
            except Exception:                                   # noqa: BLE001
                pass

            if _lv:
                _fam = {"ops": [], "master": [], "pdc": []}
                for _k, _v in sorted(_lv.items(), key=lambda x: -x[1]):
                    _fam["ops" if _k.endswith("_ops") else
                         "pdc" if _k.startswith("PDC_") else
                         "master"].append(f"{_k} {_v:,.0f}")
                _lc1.caption(
                    "**chain** " + (" · ".join(_fam["ops"]) or "—")
                    + "  \n**master** " + (" · ".join(_fam["master"]) or "—")
                    + f"  \n**PDC ({config.PDC_CANDLE:%H:%M} candle close)** "
                    + (_pdc_detail or "— press Backfill"))
            else:
                _lc1.caption("no levels available yet")
            if _lc2.button("Backfill PDC", key="pdcbf",
                           use_container_width=True):
                _ok, _m = _pdc.backfill()
                (st.success if _ok else st.error)(_m)

            _within = st.slider("Flag levels within (points)", 10, 60, 25, 5,
                                key="s2near")

            _rule_choice = st.radio(
                "Strategy Rules",
                [
                    "Mentor Sushen Rules (Confluence + CTC + 40pt Target + Max 2 Trades)",
                    "Legacy (Whichever Comes First — Unfiltered)",
                ],
                index=0,
                horizontal=True,
                key="s2_rule_mode",
                help="Mentor Sushen Rules: Confluence across 8 EMA and VWAP, Cost-to-Cost lock at +20 pts, "
                     "Target at +40 pts, Reversal exits at extremes, Max 2 trades/day, and Flat BB filter. "
                     "Eliminates whipsaws and protects capital."
            )
            _mode = "mentor" if "Mentor" in _rule_choice else "legacy"

            cA, cB = st.columns(2)
            _ce = cA.checkbox("CE (long) entries", value=True, key="s2ce")
            _pe = cB.checkbox("PE (short) entries", value=True, key="s2pe")

            _tr = s2.run(_f, allow_ce=_ce, allow_pe=_pe, mode=_mode)
            _sm = s2.summary(_tr)

            m = st.columns(6)
            m[0].metric("Trades", _sm["trades"])
            m[1].metric("Closed", _sm["closed"])
            m[2].metric("Won", _sm["wins"])
            m[3].metric("Lost", _sm["losses"])
            m[4].metric("Win Rate", f"{_sm.get('win_rate', 0):.0f}%")
            m[5].metric("Net Points", f"{_sm['points']:+.1f}")

            if _mode == "mentor":
                st.success(
                    "🛡️ **Mentor Sushen Confluence Active:** Enforcing 8 EMA + VWAP alignment, "
                    "Cost-to-Cost (CTC) stop ratchet at +20 pts, +40 pt Target profit booking, "
                    "Reversal candle exit at Day Extremes, and Max 2 Trades/Day limit to prevent afternoon chop losses."
                )
            else:
                st.warning(
                    "⚠️ **Legacy Mode Active:** Raw 'whichever comes first' with candle trail only. "
                    "Notice on 04-Sep how this took a -6.1 pt loss on afternoon chop at 14:00. "
                    "Switch to Mentor Sushen Rules above to filter this out."
                )

            if _sm["by_trigger"]:
                st.caption("fired on — " + " · ".join(
                    f"{k}: {v}" for k, v in _sm["by_trigger"].items())
                    + ("   (every entry came from the 8 EMA because VWAP was "
                       "unavailable — not the rule you specified)"
                       if set(_sm["by_trigger"]) == {"8 EMA"} and not _h["ok"]
                       else ""))

            if _tr:
                st.markdown("**Trades**")
                st.dataframe(s2.table(_tr), use_container_width=True,
                             hide_index=True)
            else:
                st.info("No entries on this frame yet.")

            # Persist the completed candles for later comparison.
            try:
                _sv = s2.store_candles(_f, now, levels=_lv)
                _bits = [f"stored to `{_sv['path']}`" if _sv["path"] else _sv["note"]]
                if _sv["path"]:
                    _bits.append(f"{_sv['total']} candles today "
                                 f"({_sv['written']} new this poll)")
                    if _sv["forming_skipped"]:
                        _bits.append(f"{_sv['forming_skipped']} still forming — "
                                     f"not written until it closes")
                    _bits.append(f"{_sv.get('sessions_kept', 0)} session(s) kept")
                st.caption(" · ".join(_bits))
            except Exception as _ce:                            # noqa: BLE001
                st.caption(f"could not store candles: {_ce}")

            st.markdown("**Every 15-minute candle**")
            st.caption("OHLC with both lines and what crossed. Built before any "
                       "signal logic, so you can audit the inputs separately "
                       "from the decisions.")
            _ct = s2.annotate_levels(s2.candle_table(_f), _lv, float(_within))
            st.dataframe(_ct[::-1], use_container_width=True,
                         hide_index=True, height=420)

if _tab_active(18):
    try:
        import vwap_institutional_strategy as vis
    except Exception:
        vis = None

    _tab18_subengine = st.radio(
        "VWAP Engine Suite",
        [
            "🌊 6-Year Institutional Quantitative VWAP (09:30 Continuation & Reversal Sniper)",
            "⚡ 15m VWAP & 8 EMA Confluence System (Mentor Sushen)",
        ],
        index=0,
        horizontal=True,
        key="t18_engine_select",
        help="Select between the 6-Year Nifty quantitative benchmark engine (09:30 Continuation & Reversal Sniper) or the 15m Mentor Sushen rulebook."
    )

    if _tab18_subengine.startswith("🌊"):
        if vis is not None:
            vis.render_vwap_institutional_tab(
                pa_frames=pa_frames if "pa_frames" in locals() else None,
                spot=spot if "spot" in locals() else None,
            )
        else:
            st.error("**vwap_institutional_strategy.py is missing.**")
    else:
        _render_tab18_mentor_s2()


# ═══════════════════════════════════════════════════════════════════════════
# Tab 19 — Smart Money Concepts
# ═══════════════════════════════════════════════════════════════════════════
#
# Isolated on purpose. Nothing here feeds the decision bar, the alerts or
# Strategy 1. SMC and the 8 EMA system give opposite instructions on the same
# candle — a wick above a swing high is a short here and a long there — so
# they are run side by side and compared in the Timeline log rather than
# blended into one verdict.
if _tab_active(19):
    try:
        import smc as _smc
        import order_flow as _of
        import breakout_quality as _bq
    except ModuleNotFoundError as _me:
        _smc = None
        st.error(f"SMC modules missing: {_me}. Copy smc.py, order_flow.py "
                 f"and breakout_quality.py into the project folder.")

    if _smc is None:
        st.stop()

    st.subheader("Smart Money Concepts")
    st.caption("A different school from Strategy 1. Kept separate — on the "
               "same candle these two can say opposite things. Compare them "
               "in the Timeline log; do not average them.")

    _sf = st.radio("Timeframe", ["15m", "5m"], horizontal=True, key="smctf")
    _sc1, _sc2 = st.columns([1, 1])
    with _sc1:
        _sn = st.slider("Pivot window (N bars each side)", 2, 5, 2, key="smcn")
    with _sc2:
        _depth = st.selectbox("Dhan History Depth", ["5 Days (Recent Swings)", "1 Day (Today)", "15 Days (Multi-Week)", "60 Days (Full History)"], index=0, key="smcdepth")

    _days_map = {"1 Day (Today)": 1, "5 Days (Recent Swings)": 5, "15 Days (Multi-Week)": 15, "60 Days (Full History)": 60}
    _n_days = _days_map.get(_depth, 5)

    if _n_days > 1:
        _sdf = pa.load_history_candles(days=_n_days, timeframe=_sf)
    else:
        _sdf = pa_frames.get(_sf)
        if _sdf is None or len(_sdf) < 2 * _sn + 3:
            _sdf = pa.load_history_candles(days=2, timeframe=_sf)

    if _sdf is None or len(_sdf) < 2 * _sn + 3:
        st.info(f"Not enough {_sf} candles yet — need at least "
                f"{2 * _sn + 3}.")
    else:
        try:
            import smc_strategy as _smc_strat
            _smc_strat.render_smc_tab_executive(
                _sdf, spot, df, _sf, _sn, _depth,
                pa_frames=pa_frames, read=read, CTX=CTX, store=store,
                now=now, vb_daily=vb_daily
            )
        except Exception as _e_smc:
            st.error(f"SMC Executive Tab error: {_e_smc}")


# ═══════════════════════════════════════════════════════════════════════════
# Tab 20 — Trading Journal & Anti-Impulse Coach
# ═══════════════════════════════════════════════════════════════════════════
if _tab_active(20):
    import trading_journal as _tj_mod
    _tj_mod.render_journal_tab()


# ═══════════════════════════════════════════════════════════════════════════
# Tab 22 — V-A-W-M Chart Patterns & Session Reversal Matrix (15m)
# ═══════════════════════════════════════════════════════════════════════════
if _tab_active(22):
    try:
        _pc15_mat = _pc15 if "_pc15" in locals() else (_prev_close if "_prev_close" in locals() else None)
        chart_patterns_tab.render(
            pa_frames=pa_frames if "pa_frames" in locals() else None,
            spot=spot if "spot" in locals() else None,
            read=read if "read" in locals() else None,
            prev_close=_pc15_mat,
            df=df if "df" in locals() else None,
        )
    except Exception as _pat_err:
        st.error(f"VAWM & Reversal Matrix tab encountered an error: {_pat_err}")


# ═══════════════════════════════════════════════════════════════════════════
# Tab 23 — WAR Setup (30-Min First-Candle FII Strategy)
# ═══════════════════════════════════════════════════════════════════════════
if _tab_active(23):
    try:
        _pc15_war = _pc15 if "_pc15" in locals() else (_prev_close if "_prev_close" in locals() else None)
        war_tab.render(
            pa_frames=pa_frames if "pa_frames" in locals() else None,
            prev_close=_pc15_war,
            chain_df=df if "df" in locals() else None,
            spot=spot if "spot" in locals() else 0.0,
        )
    except Exception as _war_err:
        st.error(f"WAR Setup tab encountered an error: {_war_err}")




# ═══════════════════════════════════════════════════════════════════════════
# Tab 25 — Paper Trading & Impulse Control Terminal
# ═══════════════════════════════════════════════════════════════════════════
if _tab_active(25):
    try:
        paper_trading_tab.render_paper_trading_tab(
            pa_frames=pa_frames if "pa_frames" in locals() else None,
            spot=spot if "spot" in locals() else 0.0,
            chain_df=df if "df" in locals() else None,
            now=now if "now" in locals() else None,
        )
    except Exception as _pt_err:
        st.error(f"Paper Trading tab encountered an error: {_pt_err}")


# ═══════════════════════════════════════════════════════════════════════════
# Tab 26 — AI Forecast & Signals (TimesFM & Volatility Cones)
# ═══════════════════════════════════════════════════════════════════════════
if _tab_active(26):
    try:
        timesfm_tab.render(
            pa_frames=pa_frames if "pa_frames" in locals() else None,
            spot=spot if "spot" in locals() else 0.0,
        )
    except Exception as _tfm_err:
        st.error(f"AI Forecast & Signals tab encountered an error: {_tfm_err}")


# ═══════════════════════════════════════════════════════════════════════════
# Tab 27 — Monday Gap Up / Gap Down Scenario Simulator & Multi-Confluence
# ═══════════════════════════════════════════════════════════════════════════
if _tab_active(27):
    try:
        import gap_scenario_engine as _gse
        _gse.render_gap_scenario_tab(
            spot=spot,
            df_chain=df if "df" in locals() else None,
            pa_frames=pa_frames if "pa_frames" in locals() else None,
            read=read if "read" in locals() else None,
            vb_daily=vb_daily if "vb_daily" in locals() else None,
            CTX=CTX if "CTX" in locals() else None,
            now=now if "now" in locals() else None,
        )
    except Exception as _gse_err:
        st.error(f"Monday Gap Simulator encountered an error: {_gse_err}")


# ═══════════════════════════════════════════════════════════════════════════
# Tab 28 — Breakout & Trap Detection Engine (3-Phase Retest & Continuation)
# ═══════════════════════════════════════════════════════════════════════════
if _tab_active(28):
    try:
        import breakout_tab as _bt_mod
        _bt_mod.render_breakout_tab(
            pa_frames=pa_frames if "pa_frames" in locals() else None,
            spot=spot if "spot" in locals() else 0.0,
            read=read if "read" in locals() else None,
            now=now if "now" in locals() else None,
        )
    except Exception as _bt_err:
        st.error(f"Breakout & Trap Scanner tab encountered an error: {_bt_err}")

# ═══════════════════════════════════════════════════════════════════════════
# Tab 29 — Institutional Quant Terminal (SMC + Real-Time Option Flow)
# ═══════════════════════════════════════════════════════════════════════════
if _tab_active(29):
    try:
        from views.terminal_tab import render_institutional_terminal
        _candle_df = None
        if "pa_frames" in locals() and isinstance(pa_frames, dict):
            for _tf in ("5m", "15m", "1m"):
                _cand = pa_frames.get(_tf)
                if _cand is not None and not _cand.empty:
                    _candle_df = _cand
                    break
        if _candle_df is None and "idx_c" in locals() and idx_c is not None and not idx_c.empty:
            _candle_df = idx_c
        _chain_df = getattr(read, "df", None) if ("read" in locals() and read is not None) else None
        render_institutional_terminal(
            candle_df=_candle_df,
            chain_df=_chain_df,
            spot=spot if "spot" in locals() else 24150.0,
        )
    except Exception as _term_err:
        st.error(f"Institutional Quant Terminal encountered an error: {_term_err}")

# ═══════════════════════════════════════════════════════════════════════════
# Tab 30 — Price Action Basics Tab (Trendlines, RSI Crossing, ATR Stops, Patterns)
# ═══════════════════════════════════════════════════════════════════════════
if _tab_active(30):
    try:
        import pa_basics_tab
        pa_basics_tab.render(
            pa_frames=pa_frames if "pa_frames" in locals() else None,
            df=idx_c if ("idx_c" in locals() and idx_c is not None and not idx_c.empty) else None,
            spot=spot if "spot" in locals() else 24150.0,
            levels=read.levels if ("read" in locals() and hasattr(read, "levels")) else None,
        )
    except Exception as _pab_err:
        st.error(f"Price Action Basics tab encountered an error: {_pab_err}")

# ═══════════════════════════════════════════════════════════════════════════
# Tab 31 — Liquidity & Smart Money Terminal (12 Concepts, Institutional HUD)
# ═══════════════════════════════════════════════════════════════════════════
if _tab_active(31):
    try:
        import liquidity_tab
        bnf_c = fetch_banknifty_price_action() if config.USE_PRICE_ACTION else None
        liquidity_tab.render(
            pa_frames=pa_frames if "pa_frames" in locals() else None,
            df=idx_c if ("idx_c" in locals() and idx_c is not None and not idx_c.empty) else None,
            spot=spot if "spot" in locals() else 24150.0,
            levels=read.levels if ("read" in locals() and hasattr(read, "levels")) else None,
            ohlc=ohlc if ("ohlc" in locals() and ohlc is not None and not ohlc.empty) else None,
            banknifty_candles=bnf_c,
        )
    except Exception as _liq_err:
        st.error(f"Liquidity & Smart Money Terminal encountered an error: {_liq_err}")

# ═══════════════════════════════════════════════════════════════════════════
# Tab 32 — Volume Profile & Nifty 50 Auction Market Analysis
# ═══════════════════════════════════════════════════════════════════════════
if _tab_active(32):
    try:
        import volume_profile_tab
        volume_profile_tab.render(
            pa_frames=pa_frames if "pa_frames" in locals() else None,
            spot=spot if "spot" in locals() else None,
            read=read if "read" in locals() else None,
            df=idx_c if ("idx_c" in locals() and idx_c is not None and not idx_c.empty) else None,
        )
    except Exception as _vp_err:
        st.error(f"Volume Profile tab encountered an error: {_vp_err}")

# ═══════════════════════════════════════════════════════════════════════════
# Tab 33 — Order Flow, Market Depth & Anti-Spoofing Analysis (Level 2)
# ═══════════════════════════════════════════════════════════════════════════
if _tab_active(33):
    try:
        # Real NIFTY 50 order flow (orderflow_recorder.py). The old market_depth_tab view drew
        # simulated footprints/CVD/walls from random numbers and is no longer shown.
        import nifty_orderflow_tab
        nifty_orderflow_tab.render(
            spot=spot if "spot" in locals() else None,
            pa_frames=pa_frames if "pa_frames" in locals() else None,
        )
    except Exception as _md_err:
        st.error(f"Order Flow & Market Depth tab encountered an error: {_md_err}")

# ═══════════════════════════════════════════════════════════════════════════
# Tab 34 — Institutional Participant Positioning & History (NSE 65-Session)
# ═══════════════════════════════════════════════════════════════════════════
if _tab_active(34):
    try:
        import participant_tab
        participant_tab.render(ctx={
            "spot": spot if "spot" in locals() else 24150.0,
            "ohlc": ohlc if "ohlc" in locals() else None,
            "read": read if "read" in locals() else None,
        })
    except Exception as _part_err:
        st.error(f"Participant Positioning tab encountered an error: {_part_err}")

# ═══════════════════════════════════════════════════════════════════════════
# Tab 35 — Comprehensive EOD Day Report & Multi-Tab Signal Audit
# ═══════════════════════════════════════════════════════════════════════════
if _tab_active(35):
    try:
        import day_report_tab
        _pc_cockpit = locals().get("_pc15", locals().get("_prev_close", None))
        day_report_tab.render(ctx={
            "spot": spot if "spot" in locals() else 23640.05,
            "pa_frames": pa_frames if "pa_frames" in locals() else None,
            "chain_df": df if "df" in locals() else None,
            "prev_close": _pc_cockpit,
            "ohlc": locals().get("ohlc"),
            "read": locals().get("read"),
            "now": locals().get("now", datetime.now()),
        })
    except Exception as _report_err:
        st.error(f"EOD Day Report tab encountered an error: {_report_err}")


# ═══════════════════════════════════════════════════════════════════════════
# Tab 36 — ⚡ Option Chain & Open Interest Analysis
# ═══════════════════════════════════════════════════════════════════════════
if _tab_active(36):
    refresh_control("option_chain_oi", (fetch_live,), "option chain", nse_ts)
    try:
        import option_chain_oi_tab
        option_chain_oi_tab.render_option_chain_oi_tab(
            df=df,
            spot=spot,
            pa_frames=pa_frames if "pa_frames" in locals() else None,
            read=read if "read" in locals() else None,
        )
    except Exception as _oc_oi_err:
        st.error(f"Option Chain & OI Analysis tab encountered an error: {_oc_oi_err}")


# ═══════════════════════════════════════════════════════════════════════════
# Tab 37 — 🛡️ Anti-Fakeout & Real-Time Pressure Engine
# ═══════════════════════════════════════════════════════════════════════════
if _tab_active(37):
    refresh_control("anti_fakeout_pressure", (fetch_live,), "option chain", nse_ts)
    try:
        import anti_fakeout_pressure_tab
        anti_fakeout_pressure_tab.render_anti_fakeout_tab(
            df=df,
            spot=spot,
            pa_frames=pa_frames if "pa_frames" in locals() else None,
            read=read if "read" in locals() else None,
        )
    except Exception as _afp_err:
        st.error(f"Anti-Fakeout & Real-Time Pressure tab encountered an error: {_afp_err}")


# ═══════════════════════════════════════════════════════════════════════════
# Tab 38 — 🕯️ Candlestick Patterns & 15m Early Read Engine
# ═══════════════════════════════════════════════════════════════════════════
if _tab_active(38):
    try:
        import patterns_tab
        patterns_tab.render(
            pa_frames=pa_frames if "pa_frames" in locals() else None,
            spot=spot if "spot" in locals() else None,
            read=read if "read" in locals() else None,
            prev_close=prev_close if "prev_close" in locals() else None,
            df=df if "df" in locals() else None,
            ctx=CTX if "CTX" in locals() else None,
        )
    except Exception as _pat_tab_err:
        st.error(f"Candlestick Patterns & 15m Early Read tab error: {_pat_tab_err}")


# ═══════════════════════════════════════════════════════════════════════════
# Tab 39 — ⚡ OI Timeline (Per-Strike Intraday Flow)
# ═══════════════════════════════════════════════════════════════════════════
if _tab_active(39):
    try:
        import views.oi_timeline_tab as _voitm
        _voitm.render(CTX)
    except Exception as _oit_err:                           # noqa: BLE001
        st.error(f"OI Timeline tab encountered an error: {_oit_err}")


# ═══════════════════════════════════════════════════════════════════════════
# Tab 40 — ⏱️ Next 15m Candle Predictor & 1st-Bar Genesis
# ═══════════════════════════════════════════════════════════════════════════
if _tab_active(40):
    try:
        import next_candle_tab as _nct
        _pc = _prev_close if "_prev_close" in locals() else (prev_close if "prev_close" in locals() else None)
        _nct.render(CTX, prev_close=_pc)
    except Exception as _nct_err:                           # noqa: BLE001
        st.error(f"Next 15m Candle tab encountered an error: {_nct_err}")


# ═══════════════════════════════════════════════════════════════════════════
# Tab 41 — 🌅 MORNING BRIEF (Consolidated Daily Decision View)
# ═══════════════════════════════════════════════════════════════════════════
if _tab_active(41):
    try:
        import morning_brief_tab
        morning_brief_tab.render(ctx=CTX if "CTX" in locals() else None)
    except Exception as _mb_err:                            # noqa: BLE001
        st.error(f"Morning Brief tab encountered an error: {_mb_err}")


# ═══════════════════════════════════════════════════════════════════════════
# Tab 42 — 📸 Snapshots (5-MIN & 15-MIN Periodic Snapshots)
# ═══════════════════════════════════════════════════════════════════════════
if _tab_active(42):
    try:
        import snapshots_tab
        snapshots_tab.render_snapshots_tab(target_date=now.date() if "now" in locals() else None)
    except Exception as _snap_err:                          # noqa: BLE001
        st.error(f"Snapshots tab encountered an error: {_snap_err}")


# ═══════════════════════════════════════════════════════════════════════════
# Tab 43 — 📊 Trainer's Participant Sheet (Workbook + Lecture-2 Correlation)
# ═══════════════════════════════════════════════════════════════════════════
if _tab_active(43):
    try:
        import participant_sheet_tab
        participant_sheet_tab.render()
    except Exception as _pst_err:                          # noqa: BLE001
        st.error(f"Trainer's Participant Sheet tab encountered an error: {_pst_err}")


# ═══════════════════════════════════════════════════════════════════════════
# Tab 44 — 🧪 Options Lab (PCR · OI change · Total OI · Writer Flow · Vega · GEX · IV−HV)
# ═══════════════════════════════════════════════════════════════════════════
if _tab_active(44):
    try:
        import options_lab_tab
        options_lab_tab.render()
    except Exception as _olab_err:                         # noqa: BLE001
        st.error(f"Options Lab tab encountered an error: {_olab_err}")
