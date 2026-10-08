"""
straddle_pro.py — "📉 Straddle" sub-tab of 🧪 Options Lab (StockMojo's ATM Straddle Chart + Multi-Straddle, plus a pro read).

  Left    strike table at the selected time: Call · Put · Straddle (ATM ± 6), pick a strike or Auto Rolling Straddle.
  Chart   straddle premium (area) with the NIFTY future (dotted) · previous session alongside · 1m / 3m / 5m / 15m.
          Multi-straddle: add up to 3 fixed strikes as extra lines.
  Read    · straddle now vs open / day low / day high, % change, decay per hour
          · ACTUAL change vs PURE TIME DECAY (θ of the ATM call + put × time) → the part that is MOVE / IV:
              straddle falling faster than θ → IV being crushed, sellers' day · straddle above its θ path → the market is
              paying for movement (trend / event) — buyers can win
          · expansion bursts (straddle +X % in 5 min with the future moving) = breakout moments, with time & direction
          · what the straddle prices for the rest of today and to expiry (implied move)
          → decision: BUYERS' DAY / SELLERS' DAY / WAIT, and how to trade it as an option BUYER.
Auto Rolling = ATM of every snapshot (it jumps when the ATM strike changes — those jumps are marked, not moves).
Information only — not financial advice. Not backtested in this app.
"""
from __future__ import annotations

import math
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

import options_lab_tab as ol
from options_lab_tab import NAVY, GOLD, WHITE, MUTED, GREEN, RED, FUT

TF = {"1m": 1, "3m": 3, "5m": 5, "15m": 15}
BREAKS = [dict(bounds=["sat", "mon"]), dict(bounds=[15.55, 9.2], pattern="hour")]
BLUE, PINK, PURPLE, TEAL = "#3b82f6", "#ec4899", "#a78bfa", "#2dd4bf"


def _exp_of(snaps: list) -> str:
    for _, _, df in reversed(snaps):
        if df is not None and "expiry" in df and len(df):
            return str(ol._one_expiry(df)["expiry"].iloc[0])
    return ""


def wide(snaps: list) -> pd.DataFrame:
    """Long frame ts, spot, strike, ce, pe, ce_theta, pe_theta (one expiry = the nearest of the session's last snapshot)."""
    exp = _exp_of(snaps)
    rows = []
    for ts, spot, df in snaps:
        if df is None or df.empty:
            continue
        d = df[df["expiry"].astype(str) == exp] if "expiry" in df and exp else ol._one_expiry(df)
        if d.empty:
            continue
        d = d.drop_duplicates("strike")
        g = lambda c: pd.to_numeric(d[c], errors="coerce") if c in d else np.nan
        rows.append(pd.DataFrame({"ts": pd.Timestamp(ts), "spot": float(spot), "strike": pd.to_numeric(d["strike"], errors="coerce"),
                                  "ce": g("ce_ltp"), "pe": g("pe_ltp"), "ce_th": g("ce_theta"), "pe_th": g("pe_theta")}))
    if not rows:
        return pd.DataFrame()
    x = pd.concat(rows, ignore_index=True).dropna(subset=["strike"])
    x["strike"] = x["strike"].round().astype(int)
    x["ts"] = pd.to_datetime(x["ts"]).astype("datetime64[ns]")
    for c in ("ce", "pe"):
        x.loc[x[c] <= 0, c] = np.nan
    x.attrs["expiry"] = exp
    return x


def series(x: pd.DataFrame, strike: int | None, step: float) -> pd.DataFrame:
    """Straddle through the session: rolling ATM (strike=None) or a fixed strike."""
    out = []
    for ts, g in x.groupby("ts", sort=True):
        spot = float(g.spot.iloc[0])
        k = int(round(spot / step) * step) if strike is None else int(strike)
        r = g[g.strike == k]
        if r.empty:
            continue
        r = r.iloc[0]
        if not (np.isfinite(r.ce) and np.isfinite(r.pe)):
            continue
        out.append(dict(ts=ts, spot=spot, k=k, ce=float(r.ce), pe=float(r.pe), st=float(r.ce + r.pe),
                        th=float(np.nansum([r.ce_th, r.pe_th]))))
    s = pd.DataFrame(out)
    if len(s):
        s["roll"] = s.k.diff().fillna(0) != 0
    return s


def add_future(s: pd.DataFrame, fut: pd.DataFrame | None) -> pd.DataFrame:
    if s.empty:
        return s
    s = s.copy()
    if fut is None or fut.empty:
        s["fut"] = s.spot
        return s
    f = fut[["ts", "fut"]].copy()
    f["ts"] = pd.to_datetime(f["ts"]).dt.tz_localize(None).astype("datetime64[ns]")
    s = pd.merge_asof(s.sort_values("ts"), f.sort_values("ts"), on="ts", direction="nearest", tolerance=pd.Timedelta(minutes=3))
    s["fut"] = s.fut.interpolate(limit_direction="both").fillna(s.spot)
    return s


def resample(s: pd.DataFrame, tf: str) -> pd.DataFrame:
    m = TF[tf]
    if m == 1 or s.empty:
        return s
    parts = []
    for _, g in s.groupby(s.ts.dt.date):
        parts.append(g.set_index("ts").resample(f"{m}min", origin="start_day", offset="15min").last().dropna(subset=["st"]))
    return pd.concat(parts).reset_index()


def future_frame(snaps: list, day: date, real: bool):
    try:
        import pcr_oi_pro as po
        if real:
            f, why = po.future_1m(day)
            if f is not None and len(f) >= 3:
                return f, "NIFTY future", why
        return po.synth(snaps), "Synth future", "synth future"
    except Exception as exc:                                    # noqa: BLE001
        return None, "NIFTY spot", f"spot ({str(exc)[:40]})"


# ═══════════════════════════════════════════════════════════════════════════
# Read
# ═══════════════════════════════════════════════════════════════════════════
def bursts(s: pd.DataFrame, minutes: int = 5, pct: float = 4.0) -> pd.DataFrame:
    """Straddle up ≥ pct % within `minutes` (ignoring ATM-roll jumps) = an expansion burst."""
    if len(s) < minutes + 2:
        return s.iloc[0:0]
    x = s.set_index("ts")
    prev = x.st.shift(minutes)
    chg = 100 * (x.st - prev) / prev
    rolled = x.roll.rolling(minutes, min_periods=1).max().astype(bool)
    b = x[(chg >= pct) & ~rolled].copy()
    b["chg"] = chg[b.index]
    b["dfut"] = (x.fut - x.fut.shift(minutes))[b.index]
    keep, last = [], None
    for t in b.index:                                           # one marker per burst (15 min apart)
        if last is None or (t - last) > pd.Timedelta(minutes=15):
            keep.append(t); last = t
    return b.loc[keep].reset_index()


def read(s: pd.DataFrame, expiry: str, step: float) -> dict:
    o, n = s.iloc[0], s.iloc[-1]
    lo_i, hi_i = s.st.idxmin(), s.st.idxmax()
    mins = max(1.0, (n.ts - o.ts).total_seconds() / 60)
    chg = n.st - o.st
    theta_path = float(np.nanmean(s.th)) * mins / 375.0          # θ per day (negative) × share of a session
    move_part = chg - theta_path
    w30 = s[s.ts >= n.ts - pd.Timedelta(minutes=30)]
    slope30 = (w30.st.iloc[-1] - w30.st.iloc[0]) / max(1, (w30.ts.iloc[-1] - w30.ts.iloc[0]).total_seconds() / 3600) if len(w30) > 1 else np.nan
    d_fut = float(n.fut - o.fut)
    bu = bursts(s)
    e = pd.to_datetime(expiry, errors="coerce", dayfirst=not str(expiry)[:4].isdigit())
    try:
        import olab_pro as op
        m_exp = op.minutes_to_expiry(n.ts, expiry)
    except Exception:                                           # noqa: BLE001
        m_exp = np.nan
    left = (n.ts.normalize() + pd.Timedelta(hours=15, minutes=30) - n.ts).total_seconds() / 60
    today_move = n.st * math.sqrt(max(0.0, left) / m_exp) if m_exp and np.isfinite(m_exp) and m_exp > 0 else np.nan
    exp_day = bool(pd.notna(e) and e.date() == n.ts.date())
    # verdict
    if move_part > 0.15 * abs(theta_path) + 2 and chg > -abs(theta_path) * 0.5:
        verdict, vcol = "BUYERS' DAY — the market is paying for movement", GREEN
    elif chg < theta_path * 1.3 and abs(d_fut) < 0.6 * o.st:
        verdict, vcol = "SELLERS' DAY — straddle melting faster than time alone (IV crush / range)", RED
    elif chg < 0:
        verdict, vcol = "NORMAL DECAY — straddle falling about as time decay predicts", GOLD
    else:
        verdict, vcol = "MIXED — straddle holding up; wait for a burst or a wall break", GOLD
    L = [f"ATM straddle <b>{n.st:,.1f}</b> (opened {o.st:,.1f}, <b style='color:{GREEN if chg > 0 else RED}'>{chg:+,.1f} "
         f"= {100 * chg / o.st:+.1f}%</b>) · day low {s.st.min():,.1f} at {s.ts[lo_i]:%H:%M} · high {s.st.max():,.1f} at "
         f"{s.ts[hi_i]:%H:%M}. Future {d_fut:+,.0f} pts over the same time.",
         f"<b>Time decay alone</b> would have taken it {theta_path:+,.1f} (θ of the ATM call + put). Actual {chg:+,.1f} → "
         f"the <b>move / IV part is {move_part:+,.1f}</b>: "
         + ("price movement and IV are paying option holders — premiums are being BID." if move_part > 2 else
            "IV is being crushed on top of decay — premiums leak even when price moves." if move_part < -2 else
            "about what decay alone explains — no extra fear or excitement priced."),
         f"Last 30 min: straddle {slope30:+,.1f} pts/hour " + (
             "— expanding: something is moving, follow the break direction." if np.isfinite(slope30) and slope30 > 2 else
             "— decaying: quiet tape, buyers bleed." if np.isfinite(slope30) and slope30 < -2 else "— flat."),
         ]
    if len(bu):
        bl = " · ".join(f"{r.ts:%H:%M} +{r.chg:.0f}% (future {r.dfut:+.0f})" for r in bu.tail(4).itertuples())
        L.append(f"<b>Expansion bursts</b> (straddle +4% in 5 min): {bl} — these were the breakout moments.")
    L.append(f"The straddle prices a move of <b>±{n.st:,.0f}</b> to expiry {expiry}"
             + (f" and about <b>±{today_move:,.0f}</b> for the rest of today" if np.isfinite(today_move) else "") + ".")
    if exp_day:
        L.append("<span style='color:#f97316'>EXPIRY DAY: the straddle goes to its intrinsic value by 15:30 — fast decay is "
                 "normal. Only bursts matter.</span>")
    do = []
    if vcol == GREEN:
        do += ["Buy options in the direction of the break (ITM, your setup), hold for the move — time decay is not the problem today.",
               "Enter on a pull-back after a burst, not at the burst high (premiums are inflated at that moment)."]
    elif vcol == RED:
        do += ["Don't hold bought options waiting — every 30 min costs premium. Quick in-and-out at walls only, or sit out.",
               "If you must buy: ITM (less θ % and less IV per ₹) and a time stop (exit in 15–20 min if it does not move)."]
    else:
        do += ["Wait for an expansion burst with a candle close through a level before buying.",
               "Prefer ITM strikes; avoid ATM / OTM while the straddle is just decaying."]
    return dict(lines=L, verdict=verdict, vcol=vcol, do=do, bursts=bu, today_move=today_move)


# ═══════════════════════════════════════════════════════════════════════════
# Chart
# ═══════════════════════════════════════════════════════════════════════════
def chart(main: pd.DataFrame, extra: dict, fut_label: str, bu: pd.DataFrame, label: str) -> go.Figure:
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Scatter(x=main.ts, y=main.fut, name=fut_label, line=dict(color=FUT, dash="dot", width=1.3),
                             hovertemplate="%{x|%d %b %H:%M}<br>" + fut_label + " %{y:,.1f}<extra></extra>"), secondary_y=False)
    fig.add_trace(go.Scatter(x=main.ts, y=main.st, name=label, fill="tozeroy", fillcolor="rgba(59,130,246,0.18)",
                             line=dict(color=BLUE, width=1.8), customdata=np.c_[main.k, main.ce, main.pe],
                             hovertemplate="Straddle %{y:,.1f} (%{customdata[0]:.0f}: CE %{customdata[1]:,.1f} + PE "
                                           "%{customdata[2]:,.1f})<extra></extra>"), secondary_y=True)
    cols = [PINK, PURPLE, TEAL]
    for (k, s_), c in zip(extra.items(), cols):
        fig.add_trace(go.Scatter(x=s_.ts, y=s_.st, name=f"{k} straddle", line=dict(color=c, width=1.3),
                                 hovertemplate=f"{k}: " + "%{y:,.1f}<extra></extra>"), secondary_y=True)
    r = main[main.get("roll", False) == True] if "roll" in main else main.iloc[0:0]   # noqa: E712
    if len(r):
        fig.add_trace(go.Scatter(x=r.ts, y=r.st, mode="markers", name="ATM rolled", marker=dict(symbol="diamond-open", color=GOLD, size=7),
                                 customdata=r.k, hovertemplate="ATM → %{customdata:.0f}<extra></extra>"), secondary_y=True)
    if len(bu):
        fig.add_trace(go.Scatter(x=bu.ts, y=bu.st, mode="markers", name="Expansion burst",
                                 marker=dict(symbol="star", color=GREEN, size=12, line=dict(color=WHITE, width=0.5)),
                                 customdata=np.c_[bu.chg, bu.dfut],
                                 hovertemplate="%{x|%H:%M} burst +%{customdata[0]:.0f}% · future %{customdata[1]:+.0f}<extra></extra>"),
                      secondary_y=True)
    days = sorted(main.ts.dt.normalize().unique())
    for d in days[1:]:
        fig.add_vline(x=pd.Timestamp(d) + pd.Timedelta(hours=9, minutes=15), line=dict(color=BLUE, dash="dot", width=1))
    ol._layout(fig, "Straddle Chart", h=470)
    fig.update_layout(margin=dict(l=10, r=10, t=50, b=10))
    fig.update_xaxes(rangebreaks=BREAKS, tickformat="%H:%M\n%d %b")
    fig.update_yaxes(title_text=fut_label, tickformat=",.0f", showgrid=False, secondary_y=False)
    vals = pd.concat([main.st] + [s_.st for s_ in extra.values()]).dropna()
    if len(vals):
        lo, hi = float(vals.min()), float(vals.max())
        pad = max(1.0, 0.08 * (hi - lo))
        fig.update_yaxes(range=[lo - pad, hi + pad], secondary_y=True)
    fig.update_yaxes(title_text="straddle (pts)", tickformat=",.1f", secondary_y=True)
    return fig


def _card(title: str, body: str, border: str = GOLD) -> str:
    return (f"<div style='background:{NAVY};border:1px solid {border};border-radius:8px;padding:10px 14px;margin:6px 0'>"
            f"<div style='color:{GOLD};font-weight:700'>{title}</div>"
            f"<div style='color:{WHITE};margin-top:4px;line-height:1.6'>{body}</div></div>")


HOW = ["<b>ATM straddle</b> = ATM call + ATM put = the price of 'NIFTY will move'. Falling = the market expects less movement "
       "(or time is passing); rising = movement / fear is being paid for.",
       "<b>Compare with pure time decay</b>: on a quiet day the straddle falls roughly by θ × time. Falling FASTER = IV crush "
       "(sellers' day). Falling SLOWER or rising = price is moving enough to beat decay (buyers' day).",
       "<b>Expansion burst</b> (★) = straddle +4 % in 5 min. That is the moment a range breaks; the future's direction at that "
       "moment is the direction to trade — on the next pull-back, not at the burst high.",
       "<b>Auto Rolling</b> follows the ATM strike; the ◇ marks are strike changes (not moves). For a clean line pick a fixed strike.",
       "<b>Implied move</b>: the straddle ≈ the average move to expiry. Rest-of-today ≈ straddle × √(minutes left ÷ minutes to expiry).",
       "Multi-straddle: a strike below the money rising faster than the ATM = put side being bid (fear); above = calls bid."]


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
    def _w(d: date, n: int):
        sn = store.load_day(d)
        return sn, wide(sn)

    step = float(ol._rules().get("strike_step", 50) or 50)
    c = st.columns([1.5, 1.3, 1.4, 1.8, 2.0])
    day = c[0].selectbox("Session", days, index=days.index(day) if day in days else 0,
                         format_func=lambda d: d.strftime("%a %d %b %Y"), key="std_day")
    tf = c[1].selectbox("Timeframe", list(TF), index=0, key="std_tf")
    prev_on = c[2].checkbox("Prev session", value=True, key="std_prev")
    src = c[3].radio("Future line", ["NIFTY future", "Synth future"], horizontal=True, key="std_src")
    snaps, x = _w(day, _n(day))
    if x.empty or x.ts.nunique() < 3:
        st.warning("Not enough snapshots for this session yet.")
        return
    exp = x.attrs.get("expiry", "")
    last = x[x.ts == x.ts.max()]
    spot = float(last.spot.iloc[0])
    atm = int(round(spot / step) * step)
    have = sorted(last.strike.unique())
    extra_k = c[4].multiselect("Multi-straddle (fixed strikes)", [k for k in have if abs(k - atm) <= 10 * step],
                               max_selections=3, key="std_multi")

    left, right = st.columns([1.05, 3])
    with left:
        tb = last[(last.strike - atm).abs() <= 6 * step].sort_values("strike")
        rows = "".join(
            f"<tr style='{'background:#1e3a5f;font-weight:700' if k == atm else ''}'>"
            f"<td style='padding:3px 6px'>{k}</td><td style='padding:3px 6px;color:{GREEN}'>{ce:,.2f}</td>"
            f"<td style='padding:3px 6px;color:{RED}'>{pe:,.2f}</td><td style='padding:3px 6px'><b>{ce + pe:,.2f}</b></td></tr>"
            for k, ce, pe in zip(tb.strike, tb.ce, tb.pe) if np.isfinite(ce) and np.isfinite(pe))
        st.markdown(f"<div style='background:{NAVY};border:1px solid {GOLD};border-radius:8px;padding:6px 8px'>"
                    f"<table style='color:{WHITE};font-size:0.84rem;width:100%'><tr style='color:{MUTED}'><th>Strike</th>"
                    f"<th>Call</th><th>Put</th><th>Straddle</th></tr>{rows}</table></div>", unsafe_allow_html=True)
        opts = ["Auto Rolling Straddle"] + [str(k) for k in tb.strike]
        pick = st.radio("Straddle", opts, index=0, key="std_pick", label_visibility="collapsed")
    strike = None if pick.startswith("Auto") else int(pick)
    fut, flabel, why = future_frame(snaps, day, src == "NIFTY future")
    s = add_future(series(x, strike, step), fut)
    if s.empty or len(s) < 3:
        right.warning("No prices for that strike yet.")
        return
    s_prev = pd.DataFrame()
    older = [d for d in days if d < day]
    if prev_on and older:
        sp_, xp = _w(older[0], _n(older[0]))
        if len(xp):
            fp, _, _ = future_frame(sp_, older[0], src == "NIFTY future")
            s_prev = add_future(series(xp, strike, step), fp)
    rd = read(s, exp, step)
    show = resample(pd.concat([s_prev, s], ignore_index=True) if len(s_prev) else s, tf)
    extra = {k: resample(add_future(series(x, k, step), fut), tf) for k in extra_k}
    label = "ATM straddle (rolling)" if strike is None else f"{strike} straddle"
    with right:
        st.caption(f"Expiry {exp} · {len(s)} snapshots · future line: {why}")
        st.plotly_chart(chart(show, extra, flabel, rd["bursts"], label), use_container_width=True, key="std_chart")
    st.markdown(_card(f"★ PRO READ — <span style='color:{rd['vcol']}'>{rd['verdict']}</span>", "<br>".join(rd["lines"]),
                      border=rd["vcol"]), unsafe_allow_html=True)
    st.markdown(_card("WHAT TO DO WITH IT", "<ul style='margin:0 0 0 18px'>" + "".join(f"<li>{d}</li>" for d in rd["do"])
                      + "</ul>"), unsafe_allow_html=True)
    with st.expander("How to read the straddle"):
        st.markdown("<ul>" + "".join(f"<li style='margin:3px 0'>{h}</li>" for h in HOW) + "</ul>", unsafe_allow_html=True)
    st.caption("Information only — not financial advice. Not backtested in this app.")
