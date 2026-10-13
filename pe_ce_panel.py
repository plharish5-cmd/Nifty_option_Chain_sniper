"""
pe_ce_panel.py — StockMojo-style "PE − CE OI" time table + Smart OI charts,
built from YOUR stored option-chain snapshots. Shown as a sub-tab inside
⚡ Option Chain & OI Analysis.

TABLE (one row per candle, latest on top)
  Put OI  : Change (Day) · Change        Call OI : Change (Day) · Change
  PE − CE : Change (Day) · Change        PE − CE bars (Day / candle)
  PCR · NIFTY · Vol PCR · READ (plain-English label for that candle)

  Change (Day) = OI now − yesterday's closing OI, summed over the strike range
  Change       = change since the previous row (this candle only)

SMART OI = PE − CE "Change" column = put OI added − call OI added in that candle.
  Green = put writers adding / call writers leaving (bullish pressure)
  Red   = call writers adding / put writers leaving (bearish pressure)

CHARTS
  1. NIFTY candles + Smart OI bars + Call / Put volume per candle
  2. Put OI Chg (Day) vs Call OI Chg (Day) + PE − CE line
  3. OI PCR vs Volume PCR

Reads stored snapshots only. Settings in strategy1_rules.json → "pe_ce_panel"
(OI unit multiplier shared with "options_lab"). Info only — not financial advice.
"""
from __future__ import annotations

import html
import json
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

import config
import options_lab_tab as ol
from options_lab_tab import fmt, NAVY, GOLD, WHITE, MUTED, GREEN, RED, BLUE, ORANGE, PURPLE, FUT

RULES_PATH = Path(__file__).parent / "strategy1_rules.json"
_DEF = {"default_tf": "3m", "default_strikes": 10, "strike_step": 50, "table_height_px": 640,
        "big_move_rows_pct": 0.6}


def _rules() -> dict:
    try:
        r = json.loads(RULES_PATH.read_text()).get("pe_ce_panel", {})
    except Exception:
        r = {}
    out = {**_DEF, **{k: v for k, v in r.items() if not k.startswith("_")}}
    out["oi_multiplier"] = float(ol._rules()["oi_multiplier"])
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 1 · Compute
# ═══════════════════════════════════════════════════════════════════════════
def strike_window(snaps: list, mode: str, n: int | None, centre: float | None = None,
                  step: float = 50) -> tuple[float, float] | None:
    """(lo, hi) strike range applied to EVERY row, like StockMojo's '(22050 - 23050)'."""
    if mode == "All" or not n:
        return None
    if mode == "Fixed" and centre:
        atm = centre
    else:                                                    # Auto ATM → latest ATM
        _, spot, df = snaps[-1]
        d = ol._one_expiry(df)
        atm = float(d.iloc[(d["strike"] - spot).abs().values.argmin()]["strike"])
    return atm - n * step, atm + n * step


def per_snapshot(snaps: list, window: tuple[float, float] | None, mult: float) -> pd.DataFrame:
    rows = []
    for ts, spot, df in snaps:
        if df is None or df.empty:
            continue
        d = ol._one_expiry(df)
        if window:
            d = d[(d["strike"] >= window[0]) & (d["strike"] <= window[1])]
        if d.empty:
            continue
        num = lambda c: pd.to_numeric(d[c], errors="coerce").fillna(0) if c in d else pd.Series(0.0, index=d.index)
        ce_oi, pe_oi = num("ce_oi").sum() * mult, num("pe_oi").sum() * mult
        has_chg = "ce_chg_oi" in d and d["ce_chg_oi"].notna().any()
        rows.append(dict(ts=pd.Timestamp(ts), spot=float(spot), ce_oi=ce_oi, pe_oi=pe_oi,
                         ce_day=num("ce_chg_oi").sum() * mult if has_chg else np.nan,
                         pe_day=num("pe_chg_oi").sum() * mult if has_chg else np.nan,
                         ce_vol=num("ce_vol").sum(), pe_vol=num("pe_vol").sum(),
                         **__import__("pece_pro").extra(d, ts)))                     # PECEPRO
    s = pd.DataFrame(rows)
    if s.empty:
        return s
    if s["ce_day"].isna().all():            # no chg column → change vs first snapshot
        s["ce_day"] = s["ce_oi"] - s["ce_oi"].iloc[0]
        s["pe_day"] = s["pe_oi"] - s["pe_oi"].iloc[0]
    return s


def table_rows(s: pd.DataFrame, tf_min: int) -> pd.DataFrame:
    """Bucket snapshots into candles (label = candle start, value = last in candle)."""
    x = s.set_index("ts")
    t = x.resample(f"{tf_min}min", origin="start_day", offset="15min").agg(
        open=("spot", "first"), high=("spot", "max"), low=("spot", "min"), close=("spot", "last"),
        ce_oi=("ce_oi", "last"), pe_oi=("pe_oi", "last"), ce_day=("ce_day", "last"), pe_day=("pe_day", "last"),
        ce_vol=("ce_vol", "last"), pe_vol=("pe_vol", "last")).dropna(subset=["close"]).reset_index()
    t = t[t["ts"].dt.time >= __import__("datetime").time(9, 15)].reset_index(drop=True)   # drop pre-open (09:00–09:12) rows
    t["pe_ce_day"] = t["pe_day"] - t["ce_day"]
    # first row's "Change" = its Change (Day), exactly like StockMojo's 09:15 row
    for a, b in (("pe_chg", "pe_day"), ("ce_chg", "ce_day"), ("pe_ce_chg", "pe_ce_day")):
        t[a] = t[b].diff().fillna(t[b])
    t["smart_oi"] = t["pe_ce_chg"]
    t["call_vol"] = t["ce_vol"].diff().fillna(t["ce_vol"]).clip(lower=0)
    t["put_vol"] = t["pe_vol"].diff().fillna(t["pe_vol"]).clip(lower=0)
    t["pcr"] = t["pe_oi"] / t["ce_oi"].replace(0, np.nan)
    t["vol_pcr"] = t["pe_vol"] / t["ce_vol"].replace(0, np.nan)
    t["px_chg"] = t["close"].diff().fillna(t["close"] - t["open"])
    t["read"] = [_label(r) for r in t.itertuples()]
    t = __import__("pece_pro").add(t, s, tf_min)                                     # PECEPRO
    return t


MEANING = {
    "Short covering ▲": "Call writers buying back as price rises — fuels the up-move",
    "Put writing ▲": "Put writers adding as price holds/rises — floor being built below",
    "Dip defended ▲": "Price dipped but puts were sold into it — writers defending support",
    "Call writing ▼": "Call writers adding as price falls/holds — ceiling being built above",
    "Rally sold ▼": "Price rose but calls were sold into it — writers capping the rally",
    "Put unwinding ▼": "Put writers exiting as price falls — floor giving way",
    "Calls exiting": "Call writers booking profit on a fall — ceiling thinning (weak ▲)",
    "Puts exiting": "Put writers booking profit on a rise — floor thinning (weak ▼)",
    "Mixed": "Both sides active, no clear lead",
    "—": "No meaningful OI change this candle",
}


def _label(r) -> tuple[str, str]:
    """Plain-English read for one candle → (text, 'bull'|'bear'|'flat')."""
    dp, dc, px = r.pe_chg, r.ce_chg, r.px_chg
    if abs(dp) < 1 and abs(dc) < 1:
        return "—", "flat"
    big = max(abs(dp), abs(dc))
    if dc < 0 and abs(dc) == big and px >= 0:
        return "Short covering ▲", "bull"
    if dp < 0 and abs(dp) == big and px <= 0:
        return "Put unwinding ▼", "bear"
    if dc > 0 and abs(dc) == big:
        return ("Call writing ▼" if px <= 0 else "Rally sold ▼"), "bear"
    if dp > 0 and abs(dp) == big:
        return ("Put writing ▲" if px >= 0 else "Dip defended ▲"), "bull"
    if dc < 0:
        return "Calls exiting", "bull"
    if dp < 0:
        return "Puts exiting", "bear"
    return "Mixed", "flat"


# ═══════════════════════════════════════════════════════════════════════════
# 2 · HTML table (StockMojo look)
# ═══════════════════════════════════════════════════════════════════════════
_LG, _LR = "#dcfce7", "#fee2e2"          # light green / light red cells
_DG, _DR = "#16a34a", "#dc2626"          # strong green / red
_TXT = "#0f172a"


def _cell(v, bg="", color=_TXT, bold=False, align="center"):
    return (f"<td style='background:{bg};color:{color};text-align:{align};padding:5px 8px;"
            f"{'font-weight:700;' if bold else ''}border-bottom:1px solid #e2e8f0;white-space:nowrap'>{v}</td>")


def _bar(v, vmax):
    if vmax <= 0 or pd.isna(v):
        return _cell("")
    w = max(2, abs(v) / vmax * 92)
    c = "#ef4444" if v < 0 else "#10b981"
    return _cell(f"<div style='margin:auto;width:{w:.0f}%;height:8px;border-radius:4px;background:{c}'></div>",
                 align="center")


def render_table(t: pd.DataFrame, show: str, height: int) -> str:
    d = t.iloc[::-1].reset_index(drop=True)
    mx = {c: d[c].abs().max() for c in ("pe_ce_day", "pe_ce_chg")}
    peaks = {c: d[c].idxmax() for c in ("pe_day", "ce_day", "pe_oi", "ce_oi")}
    show_day, show_tot = show in ("OI Change (Day)", "All"), show in ("Total OI", "All")

    side_cols = (["Total"] if show_tot else []) + (["Change (Day)"] if show_day else []) + ["Change"]
    sub = side_cols + side_cols + ["Change (Day)", "Change", "Change (Day)", "Change", "OI", "Vol", "Spot",
                                   "Support", "Resistance", "Level event", "Action",
                                   "Signal", "Meaning", "Context · strength"]
    grp_cols = [("Put OI", len(side_cols), "#fee2e2", "#b91c1c"), ("Call OI", len(side_cols), "#dcfce7", "#15803d"),
                ("PE − CE OI", 2, "#f3e8ff", "#7e22ce"), ("PE − CE OI Bar", 2, "#e0f2fe", "#0369a1"),
                ("PCR · table strikes", 2, "#fef9c3", "#a16207"), ("NIFTY", 1, "#e0f2fe", "#0369a1"),
                ("🎯 LEVELS · ACTION", 4, "#1e293b", "#facc15"), ("READ", 2, NAVY, GOLD), ("★ PRO READ", 1, GOLD, NAVY)]

    th = "position:sticky;z-index:2;padding:6px 8px;font-weight:600;border-bottom:1px solid #cbd5e1;white-space:nowrap"
    head1 = (f"<th rowspan=2 style='{th};top:0;background:#f1f5f9;color:{_TXT};left:0;z-index:3'>Time</th>" +
             "".join(f"<th colspan={n} style='{th};top:0;background:{bg};color:{fg}'>{name}</th>"
                     for name, n, bg, fg in grp_cols))
    head2 = "".join(f"<th style='{th};top:31px;background:#f8fafc;color:#334155'>{s}</th>" for s in sub)

    body = []
    for i, r in d.iterrows():
        cells = [f"<td style='position:sticky;left:0;background:#f8fafc;color:{_TXT};font-weight:600;"
                 f"padding:5px 8px;border-bottom:1px solid #e2e8f0'>{r.ts:%H:%M}</td>"]
        for side, chg, day, tot in (("pe", r.pe_chg, r.pe_day, r.pe_oi), ("ce", r.ce_chg, r.ce_day, r.ce_oi)):
            bg = _LG if chg >= 0 else _LR
            if show_tot:
                pk = i == peaks[f"{side}_oi"]
                cells.append(_cell(fmt(tot), _DG if pk else bg, "#fff" if pk else _TXT, pk))
            if show_day:
                pk = i == peaks[f"{side}_day"]
                cells.append(_cell(fmt(day), _DG if pk else bg, "#fff" if pk else _TXT, pk))
            cells.append(_cell(("+" if chg > 0 else "") + fmt(chg), "", _DG if chg >= 0 else _DR))
        cells.append(_cell(fmt(r.pe_ce_day), _DG if r.pe_ce_day >= 0 else _DR, "#fff", True))
        cells.append(_cell(("+" if r.pe_ce_chg > 0 else "") + fmt(r.pe_ce_chg), "", _DG if r.pe_ce_chg >= 0 else _DR))
        cells.append(_bar(r.pe_ce_day, mx["pe_ce_day"]))
        cells.append(_bar(r.pe_ce_chg, mx["pe_ce_chg"]))
        prev = d.iloc[i + 1] if i + 1 < len(d) else r
        cells.append(_cell(f"{r.pcr:.2f}", "", _DG if r.pcr >= prev.pcr else _DR))
        cells.append(_cell(f"{r.vol_pcr:.2f}" if pd.notna(r.vol_pcr) else "—", "", "#a16207"))
        cells.append(_cell(f"{r.close:,.1f}", "", _DG if r.close >= prev.close else _DR))
        cells.append(__import__("levels_read").cells(r))                               # LEVELS
        txt, k = r.read
        cells.append(_cell(html.escape(txt), {"bull": _LG, "bear": _LR}.get(k, ""),
                           {"bull": "#15803d", "bear": "#b91c1c"}.get(k, "#64748b"), k != "flat", "left"))
        cells.append(f"<td style='color:#334155;text-align:left;padding:5px 8px;border-bottom:1px solid #e2e8f0;"
                     f"white-space:normal;min-width:240px;font-size:0.8rem'>{html.escape(MEANING.get(txt, ''))}</td>")
        cells.append(__import__("pece_pro").cell(r))                                  # PECEPRO
        p_ = getattr(r, "pro", None)                                                   # FIX-8: dim noise rows
        dim = isinstance(p_, tuple) and p_[2] == "·" and not (isinstance(getattr(r, "lv", None), tuple) and r.lv[1] in ("bull", "bear"))
        body.append(f"<tr style='opacity:{0.45 if dim else 1}'>" + "".join(cells) + "</tr>")

    return (f"<div style='max-height:{height}px;overflow:auto;border:1px solid {GOLD};border-radius:8px;"
            f"background:#fff;font-size:0.84rem'><table style='border-collapse:separate;border-spacing:0;width:100%'>"
            f"<thead><tr>{head1}</tr><tr>{head2}</tr></thead><tbody>{''.join(body)}</tbody></table></div>")


# ═══════════════════════════════════════════════════════════════════════════
# 3 · Charts
# ═══════════════════════════════════════════════════════════════════════════

def _inr_axis(fig, series_list, **sel):
    """Replace SI ticks (50M) with Indian units (5Cr / 25L)."""
    vals = pd.concat([pd.Series(s, dtype=float) for s in series_list]).dropna()
    if vals.empty:
        return
    lo, hi = float(vals.min()), float(vals.max())
    if hi == lo:
        hi = lo + 1
    ticks = np.linspace(lo, hi, 6)
    fig.update_yaxes(tickvals=ticks, ticktext=[fmt(v) for v in ticks], **sel)


def chart_smart(t: pd.DataFrame, tf: str):
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, row_heights=[0.56, 0.22, 0.22], vertical_spacing=0.025)
    fig.add_trace(go.Candlestick(x=t["ts"], open=t["open"], high=t["high"], low=t["low"], close=t["close"],
                                 name="NIFTY", increasing_line_color=GREEN, decreasing_line_color=RED,
                                 increasing_fillcolor=GREEN, decreasing_fillcolor=RED), 1, 1)
    fig.add_trace(go.Bar(x=t["ts"], y=t["smart_oi"], name="Smart OI (PE−CE)",
                         marker_color=["rgba(34,197,94,0.75)" if v >= 0 else "rgba(239,68,68,0.75)" for v in t["smart_oi"]],
                         customdata=[fmt(v) for v in t["smart_oi"]],
                         hovertemplate="Smart OI %{customdata}<extra></extra>"), 2, 1)
    fig.add_trace(go.Bar(x=t["ts"], y=t["call_vol"], name="Call vol", marker_color=GREEN,
                         customdata=[fmt(v) for v in t["call_vol"]], hovertemplate="Call vol %{customdata}<extra></extra>"), 3, 1)
    fig.add_trace(go.Bar(x=t["ts"], y=t["put_vol"], name="Put vol", marker_color=RED,
                         customdata=[fmt(v) for v in t["put_vol"]], hovertemplate="Put vol %{customdata}<extra></extra>"), 3, 1)
    fig.update_layout(xaxis_rangeslider_visible=False, barmode="group", bargap=0.15)
    fig.update_yaxes(title_text="NIFTY", row=1, col=1)
    fig.update_yaxes(title_text="Smart OI", row=2, col=1)
    fig.update_yaxes(title_text="Volume", row=3, col=1)
    _inr_axis(fig, [t["smart_oi"]], row=2, col=1)
    _inr_axis(fig, [t["call_vol"], t["put_vol"]], row=3, col=1)
    clip_volume(fig, pd.concat([t["call_vol"], t["put_vol"]]), row=3, col=1)                # FIX-6
    add_levels(fig, t, row=1, col=1)                                                        # LEVELS
    return ol._layout(fig, f"NIFTY · Smart OI · Volume ({tf})", h=640)


def clip_volume(fig, v, **sel):
    """FIX-6: the 09:15 bar is often 20× the rest — cap the axis at the 98th percentile so the day is readable."""
    v = pd.Series(v, dtype=float).dropna()
    if len(v) > 10:
        cap = float(v.quantile(0.98)) * 1.3
        if cap > 0 and float(v.max()) > cap:
            ticks = np.linspace(0, cap, 5)
            fig.update_yaxes(range=[0, cap], tickvals=ticks, ticktext=[fmt(x) for x in ticks], **sel)


def add_levels(fig, t: pd.DataFrame, row=1, col=1, **kw):
    """LEVELS: support / resistance step lines + ⬆⬇ break and 🟢🔴 building markers on the price pane."""
    if "sup" not in t:
        return
    sup = pd.to_numeric(pd.Series(t["sup"]), errors="coerce")
    res = pd.to_numeric(pd.Series(t["res"]), errors="coerce")
    fig.add_trace(go.Scatter(x=t["ts"], y=sup, name="Support (max put OI)", mode="lines",
                             line=dict(color=GREEN, width=1.6, dash="dot", shape="hv"),
                             hovertemplate="Support %{y:,.0f}<extra></extra>"), row, col, **kw)
    fig.add_trace(go.Scatter(x=t["ts"], y=res, name="Resistance (max call OI)", mode="lines",
                             line=dict(color=RED, width=1.6, dash="dot", shape="hv"),
                             hovertemplate="Resistance %{y:,.0f}<extra></extra>"), row, col, **kw)
    ev = t["lv"].map(lambda x: x[2] if isinstance(x, tuple) else "")
    txt = t["lv"].map(lambda x: x[0] if isinstance(x, tuple) else "")
    spec = {"break_up": ("triangle-up", GREEN, 16, "⬆ R broken", "high", 8),
            "break_dn": ("triangle-down", RED, 16, "⬇ S broken", "low", -8),
            "fail": ("x", GOLD, 12, "✖ failed", "close", 0),
            "s_build": ("circle", GREEN, 7, "", "low", -4),
            "r_build": ("circle", RED, 7, "", "high", 4)}
    for k, (sym, colr, size, lab, base, off) in spec.items():
        m = ev == k
        if not m.any():
            continue
        fig.add_trace(go.Scatter(x=t.loc[m, "ts"], y=t.loc[m, base] + off, mode="markers+text" if lab else "markers",
                                 text=[lab] * int(m.sum()) if lab else None,
                                 textposition="top center" if off >= 0 else "bottom center",
                                 textfont=dict(color=colr, size=11),
                                 name={"break_up": "Resistance broken ⬆", "break_dn": "Support broken ⬇", "fail": "Break failed ✖",
                                       "s_build": "Support being built", "r_build": "Resistance being built"}[k],
                                 marker=dict(symbol=sym, size=size, color=colr, line=dict(color=WHITE, width=1)),
                                 customdata=txt[m], hovertemplate="%{customdata}<extra></extra>"), row, col, **kw)


def chart_oi_day(t: pd.DataFrame):
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Scatter(x=t["ts"], y=t["pe_day"], name="Put OI (Chg Day)", line=dict(color=RED, width=2.4, shape="spline"),
                             customdata=[fmt(v) for v in t["pe_day"]], hovertemplate="Put %{customdata}<extra></extra>"), secondary_y=False)
    fig.add_trace(go.Scatter(x=t["ts"], y=t["ce_day"], name="Call OI (Chg Day)", line=dict(color=GREEN, width=2.4, shape="spline"),
                             customdata=[fmt(v) for v in t["ce_day"]], hovertemplate="Call %{customdata}<extra></extra>"), secondary_y=False)
    fig.add_trace(go.Scatter(x=t["ts"], y=t["pe_ce_day"], name="PE − CE (Chg Day)", line=dict(color=PURPLE, width=2.4, shape="spline"),
                             customdata=[fmt(v) for v in t["pe_ce_day"]], hovertemplate="PE−CE %{customdata}<extra></extra>"), secondary_y=True)
    fig.add_hline(y=0, line=dict(color=MUTED, width=1, dash="dot"), secondary_y=True)
    fig.update_yaxes(title_text="Put / Call OI chg", secondary_y=False)
    fig.update_yaxes(title_text="PE − CE", secondary_y=True, showgrid=False)
    _inr_axis(fig, [t["pe_day"], t["ce_day"]], secondary_y=False)
    _inr_axis(fig, [t["pe_ce_day"]], secondary_y=True)
    return ol._layout(fig, "Put vs Call OI Change (Day) + PE − CE", h=420)


def chart_pcr(t: pd.DataFrame):
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=t["ts"], y=t["pcr"], name="OI PCR", line=dict(color=BLUE, width=2.4, shape="spline")))
    fig.add_trace(go.Scatter(x=t["ts"], y=t["vol_pcr"], name="Vol PCR (Total)", line=dict(color=ORANGE, width=2.4, shape="spline")))
    fig.add_hline(y=1.0, line=dict(color=MUTED, width=1, dash="dot"), annotation_text="1.0", annotation_font_color=MUTED)
    return ol._layout(fig, "OI PCR vs Volume PCR", h=360)


# ═══════════════════════════════════════════════════════════════════════════
# 4 · Plain-English readings
# ═══════════════════════════════════════════════════════════════════════════
def read_table(t: pd.DataFrame) -> tuple[str, str]:
    last = t.iloc[-1]
    tail = t.tail(10)
    labels = [x[0] for x in tail["read"]]
    bulls = sum(1 for x in tail["read"] if x[1] == "bull")
    bears = sum(1 for x in tail["read"] if x[1] == "bear")
    common = max(set(labels), key=labels.count)
    vote = "BULLISH" if bulls - bears >= 3 else "BEARISH" if bears - bulls >= 3 else "NEUTRAL"
    lean = "put-heavy (bullish lean)" if last.pe_ce_day > 0 else "call-heavy (bearish lean)"
    up = tail["pe_ce_chg"].sum() > 0
    if last.pe_ce_day < 0:
        turn = ("but the last 10 candles are pulling it UP (call writers covering / puts adding)" if up
                else "and the last 10 candles are pushing it further DOWN")
    else:
        turn = ("and the last 10 candles are adding to it" if up
                else "but the last 10 candles are pulling it DOWN (call writers adding / puts exiting)")
    return vote, (f"PE − CE (Day) **{fmt(last.pe_ce_day)}** → {lean}, {turn} ({fmt(tail['pe_ce_chg'].sum())}). "
                  f"Last 10 candles: **{bulls} bullish / {bears} bearish** reads; most common: **{common}**. "
                  f"PCR {last.pcr:.2f}.")


def read_smart(t: pd.DataFrame) -> tuple[str, str]:
    tail = t.tail(5)
    f = tail["smart_oi"].sum()
    px = tail["close"].iloc[-1] - tail["open"].iloc[0]
    cv, pv = tail["call_vol"].sum(), tail["put_vol"].sum()
    if f > 0 and px > 0:
        s, v = "price up + green Smart OI → rally backed by writers", "BULLISH"
    elif f < 0 and px < 0:
        s, v = "price down + red Smart OI → fall backed by writers", "BEARISH"
    elif f > 0 > px:
        s, v = "price down but green Smart OI → dip being defended (watch for a bottom)", "BULLISH"
    elif f < 0 < px:
        s, v = "price up but red Smart OI → rally being sold (watch for a top)", "BEARISH"
    else:
        s, v = "no clear flow", "NEUTRAL"
    vol = f"Call vol {fmt(cv)} vs Put vol {fmt(pv)}" + (" → more put trading (hedging/fear)" if pv > cv * 1.2 else
                                                      " → more call trading" if cv > pv * 1.2 else "")
    return v, f"Last 5 candles: Smart OI **{fmt(f)}**, NIFTY {px:+.0f} pts → {s}. {vol}."


def read_oi_day(t: pd.DataFrame) -> tuple[str, str]:
    last = t.iloc[-1]
    lo_i = t["pe_ce_day"].idxmin()
    lo = t.loc[lo_i]
    rec = last.pe_ce_day - lo.pe_ce_day
    ce_hi = t["ce_day"].max()
    msg = (f"Call OI chg **{fmt(last.ce_day)}** vs Put **{fmt(last.pe_day)}**. "
           f"PE − CE bottomed at **{fmt(lo.pe_ce_day)}** at {lo.ts:%H:%M} and has recovered **{fmt(rec)}** since.")
    if ce_hi > 0 and last.ce_day < ce_hi * 0.7:
        msg += f" Call OI is down {100 - last.ce_day / ce_hi * 100:.0f}% from its peak → call writers covering (short covering)."
        v = "BULLISH"
    elif last.pe_ce_day <= lo.pe_ce_day * 0.98:
        msg += " PE − CE is at its low of the day → call writers firmly in control."
        v = "BEARISH"
    else:
        v = "BULLISH" if rec > 0 else "NEUTRAL"
    return v, msg


def read_pcr2(t: pd.DataFrame) -> tuple[str, str]:
    last, first = t.iloc[-1], t.iloc[0]
    o, vp = last.pcr, last.vol_pcr
    v = "BULLISH" if o > first.pcr + 0.05 else "BEARISH" if o < first.pcr - 0.05 else "NEUTRAL"
    return v, (f"OI PCR **{o:.2f}** (opened {first.pcr:.2f}, day low {t['pcr'].min():.2f}). Volume PCR **{vp:.2f}**. "
               + ("Volume PCR above OI PCR → today's traders bought more puts than the positions held — hedging/fear."
                  if vp > o else "Volume PCR below OI PCR → today's trading leaned to calls."))


GUIDE = {
    "table": ("Every row is one candle. Change (Day) = OI vs yesterday's close. Change = this candle only. PE − CE = Put OI minus Call OI.",
              ["Cell colours: green = OI went UP this candle, red = went DOWN (dark cell = the day's highest)",
               "PE − CE (Day) red and getting MORE negative = call writers taking control → bearish",
               "PE − CE (Day) red but getting LESS negative (green Change) = call writers covering → bullish turn",
               "The READ column labels each candle (Short covering ▲, Put writing ▲, Dip defended ▲, Call writing ▼, Rally sold ▼, Put unwinding ▼); MEANING beside it says what that label means in plain words",
               "Look for STREAKS: 4–5 same-colour Change cells in a row matters more than one big candle"]),
    "smart": ("Smart OI = PE − CE change in each candle (put OI added − call OI added). Volume = calls and puts traded in that candle.",
              ["Green bar under a green candle = writers support the rise",
               "Red bar under a red candle = writers support the fall",
               "Green bars while candles are red = dip being defended → possible bottom",
               "Red bars while candles are green = rally being sold → possible top",
               "Volume spike with a big Smart OI bar = institutions acting; spike with tiny Smart OI = just traders churning"]),
    "oi_day": ("Total fresh put writing vs call writing since the open, plus the gap between them (purple).",
               ["Green (calls) above red (puts) = call writers dominant today → bearish lean",
                "Purple line falling = turning more bearish; rising = turning more bullish",
                "Green line FALLING fast while price rises = short covering → strongest up moves",
                "Purple at the day's low and still falling = no sign of a bottom yet"]),
    "pcr": ("OI PCR = positions held (slow). Volume PCR = what traded today (fast).",
            ["OI PCR rising through the day = put writers adding = bullish lean",
             "Volume PCR > 1 = more puts than calls traded today = hedging / fear",
             "Volume PCR spiking while price falls, then turning down = fear peaking → bounce often follows",
             "Both PCRs falling together = call side taking over = bearish"]),
}


def _panel(key, vote, now_text):
    import streamlit as st
    what, rules = GUIDE[key]
    c = {"BULLISH": GREEN, "BEARISH": RED}.get(vote, GOLD)
    li = "".join(f"<li>{x}</li>" for x in rules)
    st.markdown(
        f"""<div style="background:{NAVY};border-left:4px solid {GOLD};border-radius:6px;
        padding:12px 16px;margin:-4px 0 22px 0;color:{WHITE};font-size:0.92rem;line-height:1.5">
        <div><span style="color:{GOLD};font-weight:700">WHAT IT IS · </span>{what}</div>
        <div style="margin-top:8px"><span style="color:{GOLD};font-weight:700">RIGHT NOW · </span>
        <span style="background:{c};color:#000;font-weight:700;padding:1px 8px;border-radius:4px">{vote}</span>
        &nbsp;{ol._md_bold(now_text)}</div>
        <div style="margin-top:8px;color:{GOLD};font-weight:700">HOW TO READ</div>
        <ul style="margin:4px 0 0 18px;color:#cbd5e1">{li}</ul></div>""", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════════
# 5 · Render
# ═══════════════════════════════════════════════════════════════════════════
def render() -> None:
    import streamlit as st
    import store

    r = _rules()
    days = ol._days_available()
    if not days:
        st.info("No option-chain snapshots stored yet.")
        return

    c = st.columns([1.6, 2.2, 1.8, 1.6, 2.6, 2.2])
    day = c[0].selectbox("Session", days, format_func=lambda d: d.strftime("%d %b %Y"), key="pece_day")
    tfs = ["1m", "3m", "5m", "15m", "1h"]
    tf = c[1].radio("Candle", tfs, index=tfs.index(r["default_tf"]), horizontal=True, key="pece_tf")
    mode = c[2].radio("Mode", ["Auto ATM", "Fixed", "All"], horizontal=True, key="pece_mode")
    opts = [5, 10, 15, 20]
    n = c[3].selectbox("± Strikes", opts, index=opts.index(r["default_strikes"]) if r["default_strikes"] in opts else 1,
                       key="pece_n", disabled=(mode == "All"))
    show = c[4].radio("Show", ["OI Change (Day)", "Total OI", "All"], horizontal=True, key="pece_show")
    view = c[5].radio("View", ["Table", "Chart", "Table + Chart"], index=2, horizontal=True, key="pece_view")

    @st.cache_data(ttl=60, show_spinner=False)
    def _snaps(d: date, n_files: int):
        return store.load_day(d)

    n_files = len(list((Path(config.SNAPSHOT_DIR) / day.strftime("%Y-%m-%d")).glob("*.meta.json")))
    snaps = _snaps(day, n_files)
    if len(snaps) < 3:
        st.warning("Not enough snapshots for this session yet.")
        return
    centre = None
    if mode == "Fixed":
        spot = snaps[-1][1]
        centre = st.number_input("Centre strike", value=float(round(spot / r["strike_step"]) * r["strike_step"]),
                                 step=float(r["strike_step"]), key="pece_centre")
    win = strike_window(snaps, mode, None if mode == "All" else n, centre, r["strike_step"])
    s = per_snapshot(snaps, win, r["oi_multiplier"])
    if s.empty:
        st.warning("No strikes inside that range.")
        return
    t = table_rows(s, {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "1h": 60}[tf])
    rng = f"({win[0]:,.0f} – {win[1]:,.0f})" if win else "(all strikes)"
    st.caption(f"{len(t)} candles · range {rng} · NIFTY {t.close.iloc[-1]:,.1f} · OI ×{r['oi_multiplier']:g} · "
               f"Smart OI = PE−CE Change per candle")

    if view in ("Table", "Table + Chart"):
        st.markdown(render_table(t, show, int(r["table_height_px"])), unsafe_allow_html=True)
        with st.expander("★ How to read the PRO READ column"):                        # PECEPRO
            st.markdown("<br>".join(__import__("levels_read").GUIDE + [""] + __import__("pece_pro").GUIDE),
                        unsafe_allow_html=True)
        st.write("")
        _panel("table", *read_table(t))
    if view in ("Chart", "Table + Chart"):
        st.plotly_chart(chart_smart(t, tf), use_container_width=True, key="pece_smart")
        _panel("smart", *read_smart(t))
        st.plotly_chart(chart_oi_day(t), use_container_width=True, key="pece_oiday")
        _panel("oi_day", *read_oi_day(t))
        st.plotly_chart(chart_pcr(t), use_container_width=True, key="pece_pcr")
        _panel("pcr", *read_pcr2(t))
