"""
fii_dii_panel.py — StockMojo-style FII/DII views, from YOUR data:
  * Trainer workbook (participant_ratios.load): daily FII cash, DII cash, NIFTY close,
    and FII / PRO / Retail(Client) / DII positions (index fut, stock fut, index options)
  * NSE raw day files (data/participant_raw/<date>/day.json): adds stock options and
    FII derivatives buy/sell in ₹ Cr.

Two sub-tabs inside 🏛️ Participant Positioning & History:
  1. 💵 FII/DII Cash Market — table (Date · FII Net · DII Net · NIFTY %), NIFTY + FII + DII
     chart, monthly heatmap, plain-English reading.
  2. 🧾 FII/DII Summary — Participant × Segment table (Net OI · Prev Day · Change),
     Participant or Segment view, FII ₹ Cr card, plain-English reading.

Info only — not financial advice. Settings: strategy1_rules.json → "fii_dii_panel".
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

import config
from options_lab_tab import fmt, NAVY, PANEL, GRID, GOLD, WHITE, MUTED, GREEN, RED, BLUE, _layout, _md_bold

RULES_PATH = Path(__file__).parent / "strategy1_rules.json"
_DEF = {"table_days": 60, "chart_days": 250, "streak_note_days": 3}
RAW_DIR = Path(config.DATA_DIR) / "participant_raw"
PARTS = [("FII", "FII", "FII"), ("Pro", "PRO", "Pro"), ("Client", "Retailers", "Client"), ("DII", "DII", "DII")]
SEGS = ["Index Futures", "Index Options", "Stock Futures", "Stock Options"]


def _rules() -> dict:
    try:
        r = json.loads(RULES_PATH.read_text()).get("fii_dii_panel", {})
    except Exception:
        r = {}
    return {**_DEF, **{k: v for k, v in r.items() if not k.startswith("_")}}


def crore(x) -> str:
    return "—" if x is None or pd.isna(x) else f"{x:+,.2f}"


# ═══════════════════════════════════════════════════════════════════════════
# 1 · Data
# ═══════════════════════════════════════════════════════════════════════════
def load_cash() -> pd.DataFrame:
    """date · fii · dii · nifty · nifty_pct  (oldest first). Raw day.json values override."""
    import participant_ratios as pr
    try:
        w = pr.load()
        df = pd.DataFrame({"fii": w["fii_cash_prov"].fillna(w["fii_cash_final"]),
                           "dii": w["dii_cash"], "nifty": w["nifty"]})
    except Exception:
        df = pd.DataFrame(columns=["fii", "dii", "nifty"])
    for d, day in _raw_days().items():
        ts = pd.Timestamp(d)
        for k, v in (("fii", day.get("fii_equity_cr")), ("dii", day.get("dii_equity_cr")), ("nifty", day.get("nifty_close"))):
            if v is not None:
                df.loc[ts, k] = v
    df = df.sort_index()
    df = df.dropna(subset=["nifty"])
    df["nifty_pct"] = df["nifty"].pct_change() * 100
    return df


def _raw_days() -> dict[str, dict]:
    out = {}
    if RAW_DIR.exists():
        for p in sorted(RAW_DIR.glob("20*/day.json")):
            try:
                out[p.parent.name] = json.loads(p.read_text())
            except Exception:
                continue
    return out


def _nets_from_raw(oi: dict) -> dict:
    """oi dict for one participant (NSE file keys) → net per segment."""
    g = lambda k: float(oi.get(k, 0) or 0)
    return {
        "Index Futures": g("fut_idx_long") - g("fut_idx_short"),
        "Index Options": (g("opt_idx_call_long") - g("opt_idx_call_short")) - (g("opt_idx_put_long") - g("opt_idx_put_short")),
        "Stock Futures": g("fut_stk_long") - g("fut_stk_short"),
        "Stock Options": (g("opt_stk_call_long") - g("opt_stk_call_short")) - (g("opt_stk_put_long") - g("opt_stk_put_short")),
    }


def _nets_from_sheet(row: pd.Series) -> dict:
    return {
        "Index Futures": row.fut_idx_long - row.fut_idx_short,
        "Index Options": (row.call_long - row.call_short) - (row.put_long - row.put_short),
        "Stock Futures": row.fut_stk_long - row.fut_stk_short,
        "Stock Options": np.nan,                              # not in the workbook
    }


def positions() -> dict[pd.Timestamp, dict[str, dict]]:
    """{date: {participant: {segment: net}}} — workbook history, raw files override (they add stock options)."""
    import participant_ratios as pr
    out: dict = {}
    for label, sheet, _ in PARTS:
        try:
            s = pr._read_sheet(pr.WORKBOOK, sheet)
        except Exception:
            continue
        for d, row in s.iterrows():
            out.setdefault(pd.Timestamp(d), {})[label] = _nets_from_sheet(row)
    for d, day in _raw_days().items():
        oi = day.get("oi", {})
        rec = {}
        for label, _, rawkey in PARTS:
            if rawkey in oi:
                rec[label] = _nets_from_raw(oi[rawkey])
        if rec:
            out[pd.Timestamp(d)] = rec
    return dict(sorted(out.items()))


def fii_rupees(d: pd.Timestamp) -> dict | None:
    """FII derivatives net buy − sell in ₹ Cr for the day (from NSE fii_stats)."""
    day = _raw_days().get(f"{d:%Y-%m-%d}")
    if not day or "fii_stats" not in day:
        return None
    out = {}
    for k, name in (("INDEX FUTURES", "FII Index Futures"), ("INDEX OPTIONS", "FII Index Options"),
                    ("STOCK FUTURES", "FII Stock Futures"), ("STOCK OPTIONS", "FII Stock Options")):
        v = day["fii_stats"].get(k)
        if v and len(v) >= 4:
            out[name] = float(v[1]) - float(v[3])
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 2 · Readings
# ═══════════════════════════════════════════════════════════════════════════
def read_cash(c: pd.DataFrame) -> tuple[str, str]:
    c = c.dropna(subset=["fii"])
    if c.empty:
        return "NEUTRAL", "No FII cash data."
    last = c.iloc[-1]
    f5, d5 = c["fii"].tail(5).sum(), c["dii"].tail(5).sum()
    f20, d20 = c["fii"].tail(20).sum(), c["dii"].tail(20).sum()
    n20 = (c["nifty"].iloc[-1] / c["nifty"].iloc[-21] - 1) * 100 if len(c) > 21 else np.nan
    sign = np.sign(c["fii"].to_numpy())
    streak = 1
    for s in sign[-2::-1]:
        if s == sign[-1]:
            streak += 1
        else:
            break
    who = "selling" if last.fii < 0 else "buying"
    absorb = (d20 / -f20 * 100) if f20 < 0 and d20 > 0 else None
    pct = (c["fii"].tail(250) < last.fii).mean() * 100
    msg = (f"Today FII **{crore(last.fii)} Cr**, DII **{crore(last.dii)} Cr**. FII has been {who} for **{streak} day(s) in a row**. "
           f"Last 5 days: FII {crore(f5)} / DII {crore(d5)} Cr. Last 20 days: FII {crore(f20)} / DII {crore(d20)} Cr"
           + (f", NIFTY {n20:+.1f}%" if pd.notna(n20) else "") + ". "
           + (f"DIIs absorbed **{absorb:.0f}%** of FII selling over 20 days. " if absorb else "")
           + (f"Today's FII selling is bigger than on {100 - pct:.0f}% of the last year's days." if last.fii < 0 else
              f"Today's FII buying is bigger than on {pct:.0f}% of the last year's days."))
    vote = "BEARISH" if f5 < 0 and f20 < 0 and (absorb is None or absorb < 100) else "BULLISH" if f5 > 0 and f20 > 0 else "NEUTRAL"
    return vote, msg


def read_summary(pos: dict, d: pd.Timestamp, prev: pd.Timestamp | None) -> tuple[str, str]:
    cur = pos[d]
    pv = pos.get(prev, {}) if prev is not None else {}
    def ch(p, s):
        a, b = cur.get(p, {}).get(s), pv.get(p, {}).get(s)
        return None if a is None or b is None or pd.isna(a) or pd.isna(b) else a - b
    fif, fio = cur.get("FII", {}).get("Index Futures"), cur.get("FII", {}).get("Index Options")
    cfi = cur.get("Client", {}).get("Index Futures")
    parts = []
    vote = "NEUTRAL"
    if fif is not None:
        c = ch("FII", "Index Futures")
        side = "SHORT" if fif < 0 else "LONG"
        act = ("" if c is None else " — unchanged today" if c == 0 else
               " and ADDED longs / covered shorts today" if c > 0 else " and ADDED shorts today")
        parts.append(f"FII index futures net **{side} {fmt(abs(fif))}** contracts{act} ({fmt(c) if c is not None else '—'}).")
        vote = "BULLISH" if (c or 0) > 0 and fif > 0 else "BEARISH" if (c or 0) < 0 and fif < 0 else "NEUTRAL"
    if fio is not None:
        c = ch("FII", "Index Options")
        parts.append(f"FII index options net **{fmt(fio)}** ({'bearish' if fio < 0 else 'bullish'} book)"
                     + (f", change {fmt(c)} → {'getting more bullish' if c > 0 else 'getting more bearish'}" if c else "") + ".")
    if cfi is not None and fif is not None and np.sign(cfi) != np.sign(fif):
        parts.append(f"Clients are on the opposite side ({fmt(cfi)}) — this FII-vs-Client opposition is the normal state; "
                     f"the signal is a CHANGE in it, not its existence.")
    pro = ch("Pro", "Index Options")
    if pro:
        parts.append(f"Pro traders' index options changed {fmt(pro)} ({'adding bullish' if pro > 0 else 'adding bearish'} exposure).")
    return vote, " ".join(parts) or "No positions for this date."


# ═══════════════════════════════════════════════════════════════════════════
# 3 · Charts & tables
# ═══════════════════════════════════════════════════════════════════════════
def chart_cash(c: pd.DataFrame):
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, row_heights=[0.5, 0.25, 0.25], vertical_spacing=0.03,
                        subplot_titles=("NIFTY (daily close)", "FII Net (₹ Cr)", "DII Net (₹ Cr)"))
    fig.add_trace(go.Scatter(x=c.index, y=c["nifty"], name="NIFTY", line=dict(color=BLUE, width=2)), 1, 1)
    fig.add_trace(go.Bar(x=c.index, y=c["fii"], name="FII Net",
                         marker_color=[GREEN if v >= 0 else RED for v in c["fii"].fillna(0)]), 2, 1)
    fig.add_trace(go.Bar(x=c.index, y=c["dii"], name="DII Net",
                         marker_color=[GREEN if v >= 0 else RED for v in c["dii"].fillna(0)]), 3, 1)
    fig.add_trace(go.Scatter(x=c.index, y=c["fii"].rolling(20).sum() / 20, name="FII 20d avg",
                             line=dict(color=GOLD, width=1.5)), 2, 1)
    for a in fig.layout.annotations:
        a.font = dict(color=GOLD, size=12)
    fig.update_layout(showlegend=False, bargap=0.1)
    return _layout(fig, "FII / DII Cash Market", h=640)


def chart_heatmap(c: pd.DataFrame, col: str, title: str):
    s = c[col].dropna()
    m = s.groupby([s.index.year, s.index.month]).sum().unstack()
    m = m.reindex(columns=range(1, 13))
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    z = m.to_numpy()
    lim = np.nanmax(np.abs(z)) if np.isfinite(z).any() else 1
    fig = go.Figure(go.Heatmap(z=z, x=months, y=[str(y) for y in m.index], zmin=-lim, zmax=lim, zmid=0,
                               colorscale=[[0, RED], [0.5, "#1e293b"], [1, GREEN]],
                               text=[[("" if np.isnan(v) else f"{v/1000:+.1f}K") for v in row] for row in z],
                               texttemplate="%{text}", textfont=dict(color=WHITE, size=11),
                               hovertemplate="%{y} %{x}: %{z:,.0f} Cr<extra></extra>", showscale=False))
    fig.update_yaxes(autorange="reversed")
    return _layout(fig, title, h=60 + 34 * len(m))


def cash_table(c: pd.DataFrame, n: int) -> str:
    d = c.tail(n).iloc[::-1]
    th = "position:sticky;top:0;background:#fef9c3;color:#0f172a;padding:6px 10px;border-bottom:1px solid #cbd5e1;white-space:nowrap"
    rows = []
    for ts, r in d.iterrows():
        fc = "#16a34a" if (r.fii or 0) >= 0 else "#dc2626"
        dc = "#16a34a" if (r.dii or 0) >= 0 else "#dc2626"
        nc = "#16a34a" if (r.nifty_pct or 0) >= 0 else "#dc2626"
        rows.append(
            f"<tr><td style='padding:5px 10px;color:#0f172a;white-space:nowrap'>{ts:%a, %d %b %y}</td>"
            f"<td style='text-align:right;padding:5px 10px;color:{fc}'>{crore(r.fii)}</td>"
            f"<td style='text-align:right;padding:5px 10px;color:{dc}'>{crore(r.dii)}</td>"
            f"<td style='text-align:right;padding:5px 10px;color:#0f172a;white-space:nowrap'>{r.nifty:,.2f} "
            + (f"<span style='color:{nc}'>({r.nifty_pct:+.2f}%)</span>" if pd.notna(r.nifty_pct) else "") + "</td></tr>")
    return (f"<div style='max-height:640px;overflow:auto;border:1px solid {GOLD};border-radius:8px;background:#fff;font-size:0.85rem'>"
            f"<table style='border-collapse:collapse;width:100%'><thead><tr><th style='{th}'>Date</th>"
            f"<th style='{th}'>FII Net Cr.</th><th style='{th}'>DII Net Cr.</th><th style='{th}'>Nifty</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>")


def summary_table(pos: dict, d: pd.Timestamp, prev: pd.Timestamp | None, mode: str) -> str:
    cur, pv = pos[d], pos.get(prev, {}) if prev is not None else {}
    cell = "padding:7px 12px;border-bottom:1px solid #e2e8f0"
    th = "background:#fef9c3;color:#0f172a;padding:8px 12px;border-bottom:1px solid #cbd5e1"
    def col(v):
        return "#64748b" if v is None or pd.isna(v) else ("#16a34a" if v >= 0 else "#dc2626")
    def f(v):
        return "—" if v is None or pd.isna(v) else (("+" if v > 0 else "") + fmt(v))
    outer, inner = (["FII", "Pro", "Client", "DII"], SEGS) if mode == "Participant" else (SEGS, ["FII", "Pro", "Client", "DII"])
    rows = []
    for o in outer:
        for j, i in enumerate(inner):
            p, s = (o, i) if mode == "Participant" else (i, o)
            a = cur.get(p, {}).get(s)
            b = pv.get(p, {}).get(s)
            c = None if a is None or b is None or pd.isna(a) or pd.isna(b) else a - b
            first = f"<td rowspan={len(inner)} style='{cell};background:#fefce8;color:#0f172a;text-align:center;font-weight:600'>{o}</td>" if j == 0 else ""
            rows.append(f"<tr>{first}<td style='{cell};color:#0f172a'>{i}</td>"
                        f"<td style='{cell};text-align:center;color:{col(a)}'>{f(a)}</td>"
                        f"<td style='{cell};text-align:center;color:#0f172a'>{f(b)}</td>"
                        f"<td style='{cell};text-align:center;color:{col(c)};font-weight:600'>{f(c)}</td></tr>")
    h1, h2 = ("Participant", "Segment") if mode == "Participant" else ("Segment", "Participant")
    return (f"<div style='border:1px solid {GOLD};border-radius:8px;overflow:hidden;background:#fff;font-size:0.88rem'>"
            f"<table style='border-collapse:collapse;width:100%'><thead><tr><th style='{th}'>{h1}</th><th style='{th}'>{h2}</th>"
            f"<th style='{th}'>Net OI</th><th style='{th}'>Prev Day Net OI</th><th style='{th}'>Change OI</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>")


GUIDE = {
    "cash": ("FII = foreign institutions, DII = Indian institutions (mutual funds, insurance). Net = buy − sell in the cash (stock) market, ₹ Crore.",
             ["Red FII bars = FIIs selling Indian stocks; green = buying",
              "Long runs of red FII bars usually line up with NIFTY falling (look at Sep 2026)",
              "DII green bars absorbing FII red = cushion; if DIIs stop absorbing, falls get sharper",
              "One day means little — look at 5-day and 20-day totals and streaks",
              "Gold line = FII 20-day average: crossing above 0 is the first sign FIIs turning buyers"]),
    "heat": ("Monthly totals. Green = net buying that month, red = net selling. Darker = bigger.",
             ["See the big picture: which months FIIs sold heavily and how NIFTY did",
              "A red FII month with a green DII month = DIIs held the market up"]),
    "summary": ("Net open positions in F&O for each participant. Net = long − short contracts (options: call net − put net). "
                "Change = vs previous trading day.",
                ["FII Index Futures negative = FIIs net SHORT NIFTY futures (bearish bet); a positive Change = covering shorts",
                 "FII Index Options negative = bearish options book (more puts bought / calls sold)",
                 "Clients (retail) are almost always on the OPPOSITE side to FIIs — that is normal, watch for changes",
                 "Pro = proprietary desks of brokers; their Index Options change is a short-term clue",
                 "DII F&O positions are mostly hedges — less useful for direction",
                 "₹ Cr card = FII derivatives buy − sell value today (money flow, not open positions)"]),
}


def _box(key, vote=None, now=""):
    import streamlit as st
    what, rules = GUIDE[key]
    c = {"BULLISH": GREEN, "BEARISH": RED}.get(vote, GOLD)
    badge = (f"<span style='background:{c};color:#000;font-weight:700;padding:1px 8px;border-radius:4px'>{vote}</span>&nbsp;"
             if vote else "")
    nowh = (f"<div style='margin-top:8px'><span style='color:{GOLD};font-weight:700'>RIGHT NOW · </span>{badge}{_md_bold(now)}</div>"
            if now else "")
    li = "".join(f"<li>{x}</li>" for x in rules)
    st.markdown(f"""<div style="background:{NAVY};border-left:4px solid {GOLD};border-radius:6px;padding:12px 16px;
        margin:6px 0 20px 0;color:{WHITE};font-size:0.92rem;line-height:1.5">
        <div><span style="color:{GOLD};font-weight:700">WHAT IT IS · </span>{what}</div>{nowh}
        <div style="margin-top:8px;color:{GOLD};font-weight:700">HOW TO READ</div>
        <ul style="margin:4px 0 0 18px;color:#cbd5e1">{li}</ul></div>""", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════════
# 4 · Render
# ═══════════════════════════════════════════════════════════════════════════
def render_cash() -> None:
    import streamlit as st
    r = _rules()
    c = load_cash()
    if c.empty:
        st.info("No FII/DII cash data found (Trainer workbook or NSE raw files).")
        return
    years = ["Last 1 year"] + [str(y) for y in sorted(c.index.year.unique(), reverse=True)] + ["All"]
    a, b = st.columns([2, 3])
    yr = a.selectbox("Period", years, key="fd_year")
    view = b.radio("View", ["Chart", "Heatmap"], horizontal=True, key="fd_view")
    cc = (c.tail(int(r["chart_days"])) if yr == "Last 1 year" else c if yr == "All" else c[c.index.year == int(yr)])
    left, right = st.columns([1.05, 2.4])
    with left:
        st.markdown(cash_table(cc, int(r["table_days"]) if yr == "Last 1 year" else len(cc)), unsafe_allow_html=True)
    with right:
        if view == "Chart":
            st.plotly_chart(chart_cash(cc), use_container_width=True, key="fd_chart")
        else:
            st.plotly_chart(chart_heatmap(c if yr == "All" else cc, "fii", "FII Net by month (₹ Cr)"), use_container_width=True, key="fd_h1")
            st.plotly_chart(chart_heatmap(c if yr == "All" else cc, "dii", "DII Net by month (₹ Cr)"), use_container_width=True, key="fd_h2")
    _box("cash", *read_cash(c))
    if view == "Heatmap":
        _box("heat")


def render_summary() -> None:
    import streamlit as st
    pos = positions()
    if not pos:
        st.info("No participant positions found.")
        return
    dates = list(pos)
    a, b = st.columns([2, 3])
    d = a.selectbox("Date", dates[::-1], format_func=lambda x: x.strftime("%a %d %b %Y"), key="fds_date")
    mode = b.radio("View Mode", ["Participant", "Segment"], horizontal=True, key="fds_mode")
    i = dates.index(d)
    prev = dates[i - 1] if i > 0 else None
    cash = load_cash()
    left, right = st.columns([1, 2.6])
    with left:
        row = cash.loc[d] if d in cash.index else None
        lines = []
        if row is not None:
            lines.append(("FII Cash Market", row.fii))
            lines.append(("DII Cash Market", row.dii))
        rup = fii_rupees(d) or {}
        lines += list(rup.items())
        nifty = (f"<div style='font-size:1.15rem;font-weight:700;color:{WHITE}'>Nifty {row.nifty:,.2f} "
                 f"<span style='color:{GREEN if row.nifty_pct >= 0 else RED};font-size:0.9rem'>({row.nifty_pct:+.2f}%)</span></div>"
                 if row is not None and pd.notna(row.nifty_pct) else "")
        body = "".join(
            f"<div style='display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid {GRID}'>"
            f"<span style='color:#cbd5e1'>{k}</span><span style='color:{GREEN if (v or 0) >= 0 else RED}'>{crore(v)}</span></div>"
            for k, v in lines)
        note = "" if rup else f"<div style='color:{MUTED};font-size:0.8rem;margin-top:6px'>FII ₹ Cr rows need the NSE raw file for this date.</div>"
        st.markdown(f"""<div style="background:{NAVY};border:1px solid {GOLD};border-radius:8px;padding:12px 16px">
            <div style="color:{MUTED};font-size:0.85rem">{d:%a, %d %b %y}</div>{nifty}
            <div style="color:{MUTED};font-size:0.75rem;text-align:right">(₹ Crores)</div>{body}{note}</div>""",
                    unsafe_allow_html=True)
    with right:
        st.markdown(summary_table(pos, d, prev, mode), unsafe_allow_html=True)
        if all(pd.isna(pos[d].get(p, {}).get("Stock Options", np.nan)) for p, _, _ in PARTS):
            st.caption("Stock Options shows — when only the Trainer workbook has this date (it has no stock-options columns).")
    _box("summary", *read_summary(pos, d, prev))
