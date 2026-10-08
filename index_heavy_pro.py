"""
index_heavy_pro.py — "🏛 NIFTY Heavyweights" sub-tab of 🧪 Options Lab (StockMojo's Index Weightage + Index Contributors
+ sector view, plus breadth and a pro read).

  Weights   the 50 stocks and their index weight — kept in data/nifty50_weights.csv (created on first run from the
            weights below; NSE rebalances, so edit that file when weights change — no code change needed).
  Live      % change of every stock (Dhan /marketfeed/quote, NSE_EQ, shares the paper desk's 1-request-per-second slot)
            → POINTS each stock adds to / takes from NIFTY ≈ NIFTY prev close × weight × % change.
  Charts    contributors (top ± points) · weightage table with change and points · sector groups · breadth through the day.
  Read      what is driving NIFTY (concentration), the top-5 heavyweights' agreement, breadth (advancers / decliners and
            WEIGHTED breadth), divergences (index up on few stocks, new low on fewer decliners) → trade / fade / wait.
Information only — not financial advice. Not backtested in this app.
"""
from __future__ import annotations

import json
import time as _time
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go

import options_lab_tab as ol
from options_lab_tab import NAVY, GOLD, WHITE, MUTED, GREEN, RED

# weight % (StockMojo index-weightage, Oct 2026) · group
W0 = [("HDFCBANK", 10.54, "Banks & Fin"), ("ICICIBANK", 8.81, "Banks & Fin"), ("RELIANCE", 7.52, "Energy & Oil"),
      ("BHARTIARTL", 5.12, "Telecom"), ("LT", 4.17, "Infra & Cap goods"), ("SBIN", 3.79, "Banks & Fin"),
      ("INFY", 3.41, "IT"), ("AXISBANK", 3.32, "Banks & Fin"), ("KOTAKBANK", 2.84, "Banks & Fin"),
      ("BAJFINANCE", 2.53, "Banks & Fin"), ("M&M", 2.51, "Auto"), ("ITC", 2.43, "FMCG & Consumer"),
      ("ETERNAL", 2.26, "FMCG & Consumer"), ("TCS", 1.97, "IT"), ("SUNPHARMA", 1.88, "Pharma & Health"),
      ("TITAN", 1.84, "FMCG & Consumer"), ("HINDUNILVR", 1.57, "FMCG & Consumer"), ("TATASTEEL", 1.48, "Metals"),
      ("MARUTI", 1.47, "Auto"), ("NTPC", 1.46, "Energy & Oil"), ("ADANIPORTS", 1.36, "Infra & Cap goods"),
      ("BEL", 1.32, "Infra & Cap goods"), ("HINDALCO", 1.32, "Metals"), ("SHRIRAMFIN", 1.29, "Banks & Fin"),
      ("BSE", 1.24, "Banks & Fin"), ("HCLTECH", 1.22, "IT"), ("ULTRACEMCO", 1.22, "Infra & Cap goods"),
      ("JSWSTEEL", 1.17, "Metals"), ("POWERGRID", 1.13, "Energy & Oil"), ("BAJAJ-AUTO", 1.12, "Auto"),
      ("GRASIM", 1.11, "Infra & Cap goods"), ("INDIGO", 1.05, "FMCG & Consumer"), ("ASIANPAINT", 1.04, "FMCG & Consumer"),
      ("COALINDIA", 0.97, "Energy & Oil"), ("BAJAJFINSV", 0.96, "Banks & Fin"), ("ADANIENT", 0.96, "Metals"),
      ("EICHERMOT", 0.94, "Auto"), ("NESTLEIND", 0.92, "FMCG & Consumer"), ("TECHM", 0.91, "IT"),
      ("ONGC", 0.85, "Energy & Oil"), ("APOLLOHOSP", 0.85, "Pharma & Health"), ("TRENT", 0.83, "FMCG & Consumer"),
      ("SBILIFE", 0.74, "Banks & Fin"), ("CIPLA", 0.74, "Pharma & Health"), ("DRREDDY", 0.73, "Pharma & Health"),
      ("MAXHEALTH", 0.70, "Pharma & Health"), ("JIOFIN", 0.68, "Banks & Fin"), ("TATACONSUM", 0.60, "FMCG & Consumer"),
      ("TMPV", 0.56, "Auto"), ("HDFCLIFE", 0.55, "Banks & Fin")]
TOP5 = ["HDFCBANK", "ICICIBANK", "RELIANCE", "BHARTIARTL", "LT"]
NIFTY_IDX_ID, BANK_IDX_ID = 13, 25


def _data_dir() -> Path:
    import config
    return Path(config.DATA_DIR)


def weights() -> pd.DataFrame:
    f = _data_dir() / "nifty50_weights.csv"
    if not f.exists():
        try:
            pd.DataFrame(W0, columns=["symbol", "weight", "group"]).to_csv(f, index=False)
        except Exception:                                       # noqa: BLE001
            pass
    try:
        w = pd.read_csv(f)
        w["symbol"] = w["symbol"].astype(str).str.strip().str.upper()
        return w[["symbol", "weight", "group"]]
    except Exception:                                           # noqa: BLE001
        return pd.DataFrame(W0, columns=["symbol", "weight", "group"])


# ── security ids (Dhan scrip master, cached once a day) ──────────────────────
def sec_ids(symbols: list[str]) -> dict:
    f = _data_dir() / "nifty50_secids.json"
    today = date.today().isoformat()
    try:
        c = json.loads(f.read_text())
        if c.get("day") == today and set(symbols) <= set(c.get("ids", {})):
            return c["ids"]
    except Exception:                                           # noqa: BLE001
        pass
    import dhan_client as dc
    df = pd.read_csv(dc.SCRIP_MASTER, low_memory=False)
    cols = {k.upper(): k for k in df.columns}
    g = lambda *n: next((cols[x] for x in n if x in cols), None)
    ex, seg, ser, sym, sid = (g("SEM_EXM_EXCH_ID"), g("SEM_SEGMENT"), g("SEM_SERIES"), g("SEM_TRADING_SYMBOL"),
                              g("SEM_SMST_SECURITY_ID"))
    m = df[(df[ex].astype(str).str.upper() == "NSE") & (df[seg].astype(str).str.upper() == "E")]
    if ser:
        m = m[m[ser].astype(str).str.upper().isin(["EQ", "BE"])]
    m = m.assign(_s=m[sym].astype(str).str.upper().str.strip())
    ids = {s: str(int(m[m._s == s][sid].iloc[0])) for s in symbols if (m._s == s).any()}
    try:
        f.write_text(json.dumps({"day": today, "ids": ids}))
    except Exception:                                           # noqa: BLE001
        pass
    return ids


# ── quotes (shares the paper desk's once-per-second Dhan quote slot) ─────────
_MEM: dict = {}


def _shared_slot():
    try:
        import paper_desk as pdk
        return pdk._SLock("quotes"), pdk.SHARED_DIR / "_quotes.pkl"
    except Exception:                                           # noqa: BLE001
        return None, None


def fetch(ids: dict, max_age: float = 15.0) -> dict:
    """{symbol: (ltp, prev_close)} + '_NIFTY', '_BANKNIFTY'. Cached max_age seconds per process."""
    if _MEM.get("t") and _time.time() - _MEM["t"] < max_age:
        return _MEM["q"]
    import pickle
    import requests
    import dhan_client as dc
    import config
    body = {"NSE_EQ": [int(v) for v in ids.values()], "IDX_I": [NIFTY_IDX_ID, BANK_IDX_ID]}
    lock, f = _shared_slot()

    def _go():
        r = requests.post(f"{dc.BASE}/marketfeed/quote", json=body, headers=dc._headers(),
                          timeout=getattr(config, "REQUEST_TIMEOUT", 10))
        r.raise_for_status()
        return r.json().get("data", {})

    if lock is not None:
        with lock:
            try:
                sh = pickle.loads(f.read_bytes()) if f.exists() else {}
            except Exception:                                   # noqa: BLE001
                sh = {}
            wait = 1.05 - (_time.time() - float(sh.get("last", 0.0)))
            if wait > 0:
                _time.sleep(min(wait, 1.1))
            data = _go()
            sh["last"] = _time.time()
            try:
                tmp = f.with_suffix(".heavy.tmp")
                tmp.write_bytes(pickle.dumps(sh))
                tmp.replace(f)
            except Exception:                                   # noqa: BLE001
                pass
    else:
        data = _go()
    inv = {v: k for k, v in ids.items()}
    out = {}

    def _px(q):
        ltp = float(q.get("last_price") or 0)
        nc = q.get("net_change")
        prev = float((q.get("ohlc") or {}).get("close") or 0)
        if nc is not None and ltp:
            prev = ltp - float(nc)
        return ltp, prev

    for sid, q in (data.get("NSE_EQ") or {}).items():
        if str(sid) in inv:
            out[inv[str(sid)]] = _px(q)
    idx = data.get("IDX_I") or {}
    for k, nm in ((NIFTY_IDX_ID, "_NIFTY"), (BANK_IDX_ID, "_BANKNIFTY")):
        q = idx.get(str(k)) or idx.get(k)
        if q:
            out[nm] = _px(q)
    _MEM.update(t=_time.time(), q=out)
    return out


def table(w: pd.DataFrame, q: dict) -> tuple[pd.DataFrame, dict]:
    n_ltp, n_prev = q.get("_NIFTY", (np.nan, np.nan))
    t = w.copy()
    t["ltp"] = t.symbol.map(lambda s: q.get(s, (np.nan, np.nan))[0])
    t["prev"] = t.symbol.map(lambda s: q.get(s, (np.nan, np.nan))[1])
    t["pct"] = 100 * (t.ltp - t.prev) / t.prev.replace(0, np.nan)
    base = n_prev if np.isfinite(n_prev) and n_prev else 22500.0
    t["pts"] = base * t.weight / 100 * t.pct / 100
    nif = dict(ltp=n_ltp, prev=n_prev, chg=n_ltp - n_prev if np.isfinite(n_ltp) else np.nan,
               pct=100 * (n_ltp - n_prev) / n_prev if np.isfinite(n_ltp) and n_prev else np.nan)
    b = q.get("_BANKNIFTY", (np.nan, np.nan))
    nif["bank_pct"] = 100 * (b[0] - b[1]) / b[1] if b[1] else np.nan
    return t, nif


def log_breadth(t: pd.DataFrame, nif: dict) -> pd.DataFrame:
    """Append one row a minute to data/breadth/<date>.csv; return today's history."""
    d = _data_dir() / "breadth"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{date.today():%Y-%m-%d}.csv"
    now = pd.Timestamp(datetime.now())
    row = dict(ts=now, nifty=nif["ltp"], adv=int((t.pct > 0).sum()), dec=int((t.pct < 0).sum()),
               wadv=float(t.loc[t.pct > 0, "weight"].sum()), banks=float(t.loc[t.group == "Banks & Fin", "pts"].sum()),
               rest=float(t.loc[t.group != "Banks & Fin", "pts"].sum()))
    try:
        h = pd.read_csv(f, parse_dates=["ts"]) if f.exists() else pd.DataFrame()
        if h.empty or (now - h.ts.iloc[-1]).total_seconds() >= 55:
            h = pd.concat([h, pd.DataFrame([row])], ignore_index=True)
            if t.pct.notna().sum() >= 25 and 9 <= now.hour < 16:
                h.to_csv(f, index=False)
        return h
    except Exception:                                           # noqa: BLE001
        return pd.DataFrame([row])


# ═══════════════════════════════════════════════════════════════════════════
# Read
# ═══════════════════════════════════════════════════════════════════════════
def pro_read(t: pd.DataFrame, nif: dict, h: pd.DataFrame) -> tuple[str, str, list, list]:
    ok = t.dropna(subset=["pct"])
    adv, dec = int((ok.pct > 0).sum()), int((ok.pct < 0).sum())
    wadv = float(ok.loc[ok.pct > 0, "weight"].sum())
    wtot = float(ok.weight.sum()) or 1
    chg = nif["chg"] if np.isfinite(nif.get("chg", np.nan)) else float(ok.pts.sum())
    sgn = np.sign(chg) or 1
    top = ok.reindex(ok.pts.abs().sort_values(ascending=False).index)
    top3 = top.head(3)
    conc = float(top3.pts.sum() / chg * 100) if chg else np.nan
    t5 = ok[ok.symbol.isin(TOP5)]
    agree = int((np.sign(t5.pct) == sgn).sum())
    grp = ok.groupby("group")["pts"].sum().sort_values()
    banks = float(grp.get("Banks & Fin", 0.0))
    L = [f"NIFTY <b style='color:{GREEN if chg > 0 else RED}'>{chg:+,.1f} pts ({nif.get('pct', np.nan):+.2f}%)</b> · "
         f"the 50 stocks explain {ok.pts.sum():+,.1f} pts (weights are a snapshot — small gaps are normal).",
         f"<b>Breadth</b>: {adv} up / {dec} down · <b>weighted breadth</b> {100 * wadv / wtot:.0f}% of the index weight is green.",
         f"<b>Top 3 movers</b> {', '.join(f'{r.symbol} {r.pts:+.1f}' for r in top3.itertuples())} = "
         f"<b>{conc:.0f}%</b> of the index move" + (" — a NARROW move, carried by a few names." if np.isfinite(conc) and conc > 60 else "."),
         f"<b>Top-5 heavyweights</b> (HDFC Bank, ICICI, Reliance, Bharti, L&T): {agree}/5 moving with NIFTY.",
         f"<b>Banks & financials</b> {banks:+,.1f} pts of {chg:+,.1f}"
         + (f" ({100 * banks / chg:.0f}%) — BANKNIFTY {nif['bank_pct']:+.2f}%" if chg and np.isfinite(nif.get('bank_pct', np.nan)) else "")
         + ". Groups: " + " · ".join(f"{g} {v:+.0f}" for g, v in grp.items())]
    div = ""
    if chg > 0 and adv < 20:
        div = "Index UP on fewer than 20 advancers — fragile, heavyweight-only rally."
    elif chg < 0 and adv > 30:
        div = "Index DOWN while most stocks are green — the fall is a few heavyweights; it can snap back when they steady."
    if len(h) >= 10:
        last, back = h.iloc[-1], h.iloc[max(0, len(h) - 31)]
        d_adv = int(last.adv - back.adv)
        L.append(f"Breadth last ~30 min: advancers {int(back.adv)} → {int(last.adv)} ({d_adv:+d}); NIFTY "
                 f"{back.nifty:,.0f} → {last.nifty:,.0f}.")
        lo_i = h.nifty.idxmin()
        if lo_i == h.index[-1] and len(h) > 30:
            prev_low = h.iloc[:-15]
            if len(prev_low) and h.dec.iloc[-1] < prev_low.loc[prev_low.nifty.idxmin(), "dec"]:
                div = (div + " " if div else "") + "NEW LOW on FEWER decliners than the earlier low — positive breadth divergence."
        hi_i = h.nifty.idxmax()
        if hi_i == h.index[-1] and len(h) > 30:
            prev_hi = h.iloc[:-15]
            if len(prev_hi) and h.adv.iloc[-1] < prev_hi.loc[prev_hi.nifty.idxmax(), "adv"]:
                div = (div + " " if div else "") + "NEW HIGH on FEWER advancers than the earlier high — negative breadth divergence."
    if div:
        L.append(f"<b style='color:{GOLD}'>⚠ {div}</b>")
    broad_dn = chg < 0 and dec >= 35 and agree >= 4
    broad_up = chg > 0 and adv >= 35 and agree >= 4
    do = []
    if broad_dn:
        v, c = "BROAD SELLING — the whole index agrees", RED
        do += ["PE trades have breadth behind them; sell CE bounces, don't buy them.",
               "Exit PE when breadth stops worsening (advancers rising while NIFTY makes a new low)."]
    elif broad_up:
        v, c = "BROAD BUYING — the whole index agrees", GREEN
        do += ["CE trades have breadth behind them; buy dips to your level.",
               "Exit CE when NIFTY makes a new high on fewer advancers."]
    elif div:
        v, c = "DIVERGENCE — index and stocks disagree", GOLD
        do += ["Don't chase the index move; it's carried by a few names. Wait for breadth to confirm or reverse.",
               "Watch the heavyweights listed above — when they turn, NIFTY turns."]
    else:
        v, c = "MIXED — no clean breadth signal", GOLD
        do += ["Trade only with a level break and the top-5 heavyweights agreeing (4 of 5)."]
    return v, c, L, do


# ═══════════════════════════════════════════════════════════════════════════
# Charts
# ═══════════════════════════════════════════════════════════════════════════
def chart_contrib(t: pd.DataFrame, n: int = 16) -> go.Figure:
    x = t.dropna(subset=["pts"])
    x = x.reindex(x.pts.abs().sort_values(ascending=False).index).head(n).sort_values("pts")
    fig = go.Figure(go.Bar(x=x.pts, y=x.symbol, orientation="h", marker_color=np.where(x.pts >= 0, GREEN, RED),
                           customdata=np.c_[x.pct, x.weight], text=[f"{v:+.1f}" for v in x.pts], textposition="outside",
                           hovertemplate="<b>%{y}</b> %{customdata[0]:+.2f}% · weight %{customdata[1]:.2f}%<br>%{x:+.1f} NIFTY pts<extra></extra>"))
    ol._layout(fig, "Index contributors — NIFTY points added / taken", h=520)
    fig.update_traces(cliponaxis=False)
    m_ = float(x.pts.abs().max() or 1) * 1.25
    fig.update_xaxes(range=[-m_ if (x.pts < 0).any() else -0.1 * m_, m_ if (x.pts > 0).any() else 0.1 * m_])
    fig.update_layout(margin=dict(l=10, r=40, t=50, b=10), showlegend=False, hovermode="closest")
    fig.update_xaxes(zeroline=True, zerolinecolor=WHITE, tickformat="+.0f")
    return fig


def chart_groups(t: pd.DataFrame) -> go.Figure:
    g = t.dropna(subset=["pts"]).groupby("group").agg(pts=("pts", "sum"), w=("weight", "sum")).sort_values("pts")
    fig = go.Figure(go.Bar(x=g.pts, y=g.index, orientation="h", marker_color=np.where(g.pts >= 0, GREEN, RED),
                           text=[f"{v:+.1f}" for v in g.pts], textposition="outside", customdata=g.w,
                           hovertemplate="<b>%{y}</b> %{x:+.1f} pts · weight %{customdata:.1f}%<extra></extra>"))
    ol._layout(fig, "Sector groups — points", h=360)
    fig.update_traces(cliponaxis=False)
    m_ = float(g.pts.abs().max() or 1) * 1.3
    fig.update_xaxes(range=[-m_ if (g.pts < 0).any() else -0.1 * m_, m_ if (g.pts > 0).any() else 0.1 * m_])
    fig.update_layout(margin=dict(l=10, r=40, t=50, b=10), showlegend=False, hovermode="closest")
    fig.update_xaxes(zeroline=True, zerolinecolor=WHITE, tickformat="+.0f")
    return fig


def chart_breadth(h: pd.DataFrame) -> go.Figure:
    from plotly.subplots import make_subplots
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Scatter(x=h.ts, y=h.adv, name="Advancers", line=dict(color=GREEN, width=1.6)), secondary_y=False)
    fig.add_trace(go.Scatter(x=h.ts, y=h.dec, name="Decliners", line=dict(color=RED, width=1.6)), secondary_y=False)
    fig.add_trace(go.Scatter(x=h.ts, y=h.nifty, name="NIFTY", line=dict(color="#9ca3af", dash="dot")), secondary_y=True)
    ol._layout(fig, "Breadth through the day", h=320)
    fig.update_yaxes(title_text="stocks", range=[0, 50], secondary_y=False)
    fig.update_yaxes(tickformat=",.0f", showgrid=False, secondary_y=True)
    return fig


def weight_html(t: pd.DataFrame) -> str:
    mx = float(t.weight.max()) or 1
    rows = []
    for r in t.sort_values("weight", ascending=False).itertuples():
        col = GREEN if (r.pct or 0) > 0 else RED if (r.pct or 0) < 0 else MUTED
        pct = f"{r.pct:+.2f}%" if np.isfinite(r.pct) else "—"
        pts = f"{r.pts:+.1f}" if np.isfinite(r.pts) else "—"
        rows.append(f"<tr><td style='padding:3px 8px;text-align:right'>{r.symbol}</td>"
                    f"<td style='padding:3px 8px;color:{GOLD}'>{r.weight:.2f}%</td>"
                    f"<td style='padding:3px 8px;width:45%'><div style='background:linear-gradient(90deg,#16a34a,#4ade80);"
                    f"height:12px;border-radius:3px;width:{100 * r.weight / mx:.0f}%'></div></td>"
                    f"<td style='padding:3px 8px;color:{col}'>{pct}</td><td style='padding:3px 8px;color:{col}'>{pts}</td></tr>")
    return (f"<div style='background:{NAVY};border:1px solid {GOLD};border-radius:8px;padding:8px 10px;max-height:560px;overflow-y:auto'>"
            f"<div style='color:{GOLD};font-weight:700;margin-bottom:4px'>NIFTY Stocks Weightage · {len(t)} stocks</div>"
            f"<table style='color:{WHITE};font-size:0.84rem;width:100%'><tr style='color:{MUTED}'><th style='text-align:right'>Stock</th>"
            f"<th>Weight</th><th>Distribution</th><th>Change</th><th>NIFTY pts</th></tr>{''.join(rows)}</table></div>")


def _card(title: str, body: str, border: str = GOLD) -> str:
    return (f"<div style='background:{NAVY};border:1px solid {border};border-radius:8px;padding:10px 14px;margin:6px 0'>"
            f"<div style='color:{GOLD};font-weight:700'>{title}</div>"
            f"<div style='color:{WHITE};margin-top:4px;line-height:1.6'>{body}</div></div>")


HOW = ["<b>Points</b> a stock adds ≈ NIFTY previous close × its weight × its % change. HDFC Bank +1% ≈ +24 NIFTY pts today; "
       "a 0.6%-weight stock +1% ≈ +1.4 pts.",
       "<b>Concentration</b>: if 3 stocks make more than 60% of the move, the move is narrow — it reverses as soon as they do.",
       "<b>Top-5 heavyweights</b> (~36% of NIFTY): trade a NIFTY breakout only when at least 4 of 5 agree.",
       "<b>Breadth</b>: 35+ of 50 on one side = broad move (trend). Index up on < 20 advancers = fragile.",
       "<b>Divergence</b>: a new NIFTY low on fewer decliners (or a new high on fewer advancers) often comes before a turn.",
       "<b>Banks & financials</b> are ~38% of NIFTY: when they drive, BANKNIFTY leads — watch it first.",
       "Weights live in data/nifty50_weights.csv — update after NSE's semi-annual rebalance."]


def render(day: date | None = None) -> None:
    import streamlit as st

    w = weights()
    c = st.columns([1.2, 1.2, 3])
    auto = c[0].toggle("Auto-refresh (30 s)", value=True, key="ihp_auto")
    if c[1].button("↻ Refresh now", key="ihp_ref"):
        _MEM.clear()

    def body():
        try:
            ids = sec_ids(w.symbol.tolist())
        except Exception as exc:                               # noqa: BLE001
            st.warning(f"Could not read Dhan's scrip master for stock ids ({str(exc)[:80]}). Showing weights only.")
            ids = {}
        q = {}
        if ids:
            try:
                q = fetch(ids)
            except Exception as exc:                           # noqa: BLE001
                st.warning(f"Live quotes unavailable ({str(exc)[:80]}) — token / market closed? Showing weights only.")
        t, nif = table(w, q)
        missing = [s for s in w.symbol if s not in ids]
        if missing:
            st.caption("No Dhan id for: " + ", ".join(missing) + " (check the symbol in data/nifty50_weights.csv)")
        if t.pct.notna().sum() < 10:
            st.markdown(weight_html(t), unsafe_allow_html=True)
            return
        h = log_breadth(t, nif)
        v, vc, L, do = pro_read(t, nif, h)
        k = st.columns(5)
        k[0].metric("NIFTY", f"{nif['ltp']:,.1f}" if np.isfinite(nif["ltp"]) else "—",
                    f"{nif['chg']:+,.1f} ({nif['pct']:+.2f}%)" if np.isfinite(nif.get("chg", np.nan)) else None)
        k[1].metric("Advancers / Decliners", f"{int((t.pct > 0).sum())} / {int((t.pct < 0).sum())}")
        k[2].metric("Weighted breadth", f"{100 * t.loc[t.pct > 0, 'weight'].sum() / t.weight.sum():.0f}% green")
        k[3].metric("Banks & Fin", f"{t.loc[t.group == 'Banks & Fin', 'pts'].sum():+.1f} pts")
        k[4].metric("BANKNIFTY", f"{nif['bank_pct']:+.2f}%" if np.isfinite(nif.get("bank_pct", np.nan)) else "—")
        a, b = st.columns([1.15, 1])
        a.plotly_chart(chart_contrib(t), use_container_width=True, key="ihp_contrib")
        b.markdown(weight_html(t), unsafe_allow_html=True)
        a2, b2 = st.columns([1, 1.15])
        a2.plotly_chart(chart_groups(t), use_container_width=True, key="ihp_groups")
        if len(h) >= 3:
            b2.plotly_chart(chart_breadth(h), use_container_width=True, key="ihp_breadth")
        else:
            b2.caption("Breadth history builds up while this tab is open during market hours (one point a minute).")
        st.markdown(_card(f"★ PRO READ — <span style='color:{vc}'>{v}</span>", "<br>".join(L), border=vc), unsafe_allow_html=True)
        st.markdown(_card("WHAT TO DO WITH IT", "<ul style='margin:0 0 0 18px'>" + "".join(f"<li>{d}</li>" for d in do) + "</ul>"),
                    unsafe_allow_html=True)
        st.caption(f"Quotes {datetime.now():%H:%M:%S} · Dhan NSE_EQ · weights from data/nifty50_weights.csv")

    if auto and hasattr(st, "fragment"):
        st.fragment(run_every=30)(body)()
    else:
        body()
    with st.expander("How to read the heavyweights"):
        st.markdown("<ul>" + "".join(f"<li style='margin:3px 0'>{h_}</li>" for h_ in HOW) + "</ul>", unsafe_allow_html=True)
    st.caption("Information only — not financial advice. Not backtested in this app.")
