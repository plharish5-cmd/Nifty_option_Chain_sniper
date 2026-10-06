"""
premium_decay_pro.py — "⏳ Premium Decay" sub-tab of 🧪 Options Lab (StockMojo's Premium Decay page, same scale & settings,
plus extras).

  Chart 1 · Premium Decay   CE Change and PE Change = Σ (premium now − premium at the first candle) over the chosen
                            strikes, in index points (same scale as StockMojo), shaded to 0, NIFTY future dotted.
                            Extra: NET (CE + PE) line = what an option SELLER of both sides has made (−) / lost (+).
  Chart 2 · Call vs Put Premium   Σ CE premium and Σ PE premium of the same strikes, future dotted.
                            Extra: ✕ where the expensive side flips, optional straddle (CE + PE) line.
  Strikes  ATM ± range (range fixed on the LATEST ATM, like StockMojo's "22600 – 22900") · Fixed strike ± range ·
           Custom strikes.     Future line: real current-month NIFTY future when available, else synth future.
  Below    plain-English read, decay speed (pts / hour, last 30 min), table view, how to use.
Information only — not financial advice.
"""
from __future__ import annotations

from datetime import date, time
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

import options_lab_tab as ol
from options_lab_tab import NAVY, GOLD, WHITE, MUTED, GREEN, RED, PURPLE, FUT

TF = {"1m": 1, "3m": 3, "5m": 5, "15m": 15}


# ═══════════════════════════════════════════════════════════════════════════
# 1 · Data
# ═══════════════════════════════════════════════════════════════════════════
def ltp_wide(snaps: list) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series, str]:
    """(CE ltp ts×strike, PE ltp ts×strike, spot by ts, expiry) — gaps forward-filled per strike."""
    rows, exp = [], ""
    for ts, spot, df in snaps:
        if df is None or df.empty:
            continue
        d = ol._one_expiry(df)
        if not exp and "expiry" in d:
            exp = str(d["expiry"].iloc[0])
        k = pd.to_numeric(d["strike"], errors="coerce")
        ce = pd.to_numeric(d.get("ce_ltp"), errors="coerce")
        pe = pd.to_numeric(d.get("pe_ltp"), errors="coerce")
        rows.append(pd.DataFrame({"ts": pd.Timestamp(ts), "spot": float(spot), "strike": k, "ce": ce, "pe": pe}))
    if not rows:
        e = pd.DataFrame()
        return e, e, pd.Series(dtype=float), exp
    x = pd.concat(rows, ignore_index=True).dropna(subset=["strike"])
    x["strike"] = x["strike"].round().astype(int)
    for c in ("ce", "pe"):
        x.loc[x[c] <= 0, c] = np.nan
    ce = x.pivot_table(index="ts", columns="strike", values="ce", aggfunc="last").sort_index().ffill()
    pe = x.pivot_table(index="ts", columns="strike", values="pe", aggfunc="last").sort_index().ffill()
    spot = x.groupby("ts")["spot"].last().sort_index()
    return ce, pe, spot, exp


def pick_strikes(ce: pd.DataFrame, spot: pd.Series, mode: str, n: int, centre: float | None,
                 custom: list[int], step: float) -> list[int]:
    have = sorted(set(ce.columns))
    if mode == "Custom":
        return [k for k in custom if k in have]
    if mode == "Fixed" and centre:
        atm = centre
    else:
        last = float(spot.iloc[-1])
        atm = min(have, key=lambda k: abs(k - last))
    return [k for k in have if atm - n * step - 1e-6 <= k <= atm + n * step + 1e-6]


def build(ce: pd.DataFrame, pe: pd.DataFrame, strikes: list[int], tf: str, fut: pd.DataFrame) -> pd.DataFrame:
    c, p = ce[strikes], pe[strikes]
    ok = c.notna().all(axis=1) & p.notna().all(axis=1)          # start once every chosen strike has a price
    c, p = c[ok], p[ok]
    if c.empty:
        return pd.DataFrame()
    t = pd.DataFrame({"ce_prem": c.sum(axis=1), "pe_prem": p.sum(axis=1)})
    m = TF[tf]
    if m > 1:
        t = t.resample(f"{m}min", origin="start_day", offset="15min").last().dropna()
    t["ce_chg"] = t["ce_prem"] - t["ce_prem"].iloc[0]
    t["pe_chg"] = t["pe_prem"] - t["pe_prem"].iloc[0]
    t["net"] = t["ce_chg"] + t["pe_chg"]
    t["straddle"] = t["ce_prem"] + t["pe_prem"]
    t = t.reset_index().rename(columns={"index": "ts"})
    f = fut.sort_values("ts").copy()
    f["ts"] = pd.to_datetime(f["ts"]).dt.tz_localize(None).astype("datetime64[ns]")   # Dhan = µs, snapshots = ns
    t["ts"] = pd.to_datetime(t["ts"]).astype("datetime64[ns]")
    t = pd.merge_asof(t.sort_values("ts"), f, on="ts", direction="nearest", tolerance=pd.Timedelta(minutes=max(3, m)))
    t["fut"] = t["fut"].interpolate(limit_direction="both")
    return t


def future_line(snaps: list, day: date, want_real: bool) -> tuple[pd.DataFrame, str, str]:
    try:
        import pcr_oi_pro as po
        if want_real:
            f, why = po.future_1m(day)
            if f is not None and len(f) >= 3:
                return f, "NIFTY future", why
        else:
            why = "Synth future chosen"
        return po.synth(snaps), "Synth future", why
    except Exception as exc:                                    # noqa: BLE001
        sp = pd.DataFrame([(pd.Timestamp(a), float(b)) for a, b, _ in snaps], columns=["ts", "fut"])
        return sp, "NIFTY spot", f"spot used ({str(exc)[:50]})"


def flips(t: pd.DataFrame) -> pd.DataFrame:
    g = (t["ce_prem"] - t["pe_prem"]).values
    band = 0.05 * float(np.nanmax(np.abs(g))) if len(g) else 0
    idx, side = [], 0
    for i, v in enumerate(g):
        now = 1 if v > band else -1 if v < -band else 0
        if now and side and now != side:
            idx.append(i)
        if now:
            side = now
    return t.iloc[idx]


# ═══════════════════════════════════════════════════════════════════════════
# 2 · Charts
# ═══════════════════════════════════════════════════════════════════════════
def _base(t, title, fut_label):
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Scatter(x=t["ts"], y=t["fut"], name=fut_label, line=dict(color=FUT, dash="dot", width=1.4),
                             hovertemplate="Future %{y:,.1f}<extra></extra>"), secondary_y=False)
    ol._layout(fig, title, h=460)
    fig.update_layout(hovermode="x unified", margin=dict(l=60, r=80, t=60, b=40),
                      legend=dict(orientation="h", x=1, xanchor="right", y=1.13))
    fig.update_yaxes(title_text="NIFTY future", tickformat=",.0f", showgrid=False, secondary_y=False)
    fig.update_yaxes(title_text="premium (index pts)", tickformat=",.0f", secondary_y=True)
    return fig


def _tag(fig, x, y, text, color):
    fig.add_annotation(x=x, y=y, yref="y2", text=f"<b>{text}</b>", showarrow=False, xanchor="left", xshift=6,
                       font=dict(color="#000", size=11), bgcolor=color, borderpad=2)


def chart_decay(t, fut_label, show_net):
    fig = _base(t, "Premium Decay — change since the first candle", fut_label)
    fig.add_trace(go.Scatter(x=t["ts"], y=t["ce_chg"], name="CE Change", fill="tozeroy", line=dict(color=GREEN, width=2.2),
                             fillcolor="rgba(34,197,94,0.15)", hovertemplate="CE change %{y:+,.1f}<extra></extra>"),
                  secondary_y=True)
    fig.add_trace(go.Scatter(x=t["ts"], y=t["pe_chg"], name="PE Change", fill="tozeroy", line=dict(color=RED, width=2.2),
                             fillcolor="rgba(239,68,68,0.15)", hovertemplate="PE change %{y:+,.1f}<extra></extra>"),
                  secondary_y=True)
    if show_net:
        fig.add_trace(go.Scatter(x=t["ts"], y=t["net"], name="NET (CE + PE)", line=dict(color=GOLD, width=2, dash="dash"),
                                 hovertemplate="Net %{y:+,.1f}<extra></extra>"), secondary_y=True)
    fig.add_hline(y=0, line=dict(color=MUTED, width=1), secondary_y=True)
    l = t.iloc[-1]
    _tag(fig, l.ts, l.ce_chg, f"{l.ce_chg:+,.1f}", GREEN)
    _tag(fig, l.ts, l.pe_chg, f"{l.pe_chg:+,.1f}", RED)
    return fig


def chart_premium(t, fut_label, show_straddle):
    fig = _base(t, "Call vs Put Premium", fut_label)
    fig.add_trace(go.Scatter(x=t["ts"], y=t["ce_prem"], name="CE", line=dict(color=GREEN, width=2.2),
                             hovertemplate="CE %{y:,.1f}<extra></extra>"), secondary_y=True)
    fig.add_trace(go.Scatter(x=t["ts"], y=t["pe_prem"], name="PE", line=dict(color=RED, width=2.2),
                             hovertemplate="PE %{y:,.1f}<extra></extra>"), secondary_y=True)
    if show_straddle:
        fig.add_trace(go.Scatter(x=t["ts"], y=t["straddle"], name="CE + PE (straddle)", line=dict(color=PURPLE, width=1.8, dash="dash"),
                                 hovertemplate="CE + PE %{y:,.1f}<extra></extra>"), secondary_y=True)
    fl = flips(t)
    if len(fl):
        fig.add_trace(go.Scatter(x=fl["ts"], y=fl["ce_prem"], mode="markers", name="Expensive side flipped",
                                 marker=dict(symbol="x", size=10, color=GOLD),
                                 hovertemplate="CE / PE premium crossed<extra></extra>"), secondary_y=True)
    l = t.iloc[-1]
    _tag(fig, l.ts, l.pe_prem, f"{l.pe_prem:,.1f}", RED)
    _tag(fig, l.ts, l.ce_prem, f"{l.ce_prem:,.1f}", GREEN)
    return fig


# ═══════════════════════════════════════════════════════════════════════════
# 3 · Reads
# ═══════════════════════════════════════════════════════════════════════════
def reads(t: pd.DataFrame, n_str: int, expiry_day: bool) -> str:
    l, f = t.iloc[-1], t.iloc[0]
    px = float(l.fut - f.fut)
    pct = lambda a, b: 100 * a / b if b else 0.0
    ce_p, pe_p = pct(l.ce_chg, f.ce_prem), pct(l.pe_chg, f.pe_prem)
    k = t[t["ts"] >= l.ts - pd.Timedelta(minutes=30)]
    hrs = max((k.ts.iloc[-1] - k.ts.iloc[0]).total_seconds() / 3600, 1 / 60) if len(k) > 1 else np.nan
    rate_ce = (k.ce_prem.iloc[-1] - k.ce_prem.iloc[0]) / hrs if len(k) > 1 else np.nan
    rate_pe = (k.pe_prem.iloc[-1] - k.pe_prem.iloc[0]) / hrs if len(k) > 1 else np.nan
    net = float(l.net)
    if l.ce_chg < 0 and l.pe_chg < 0:
        story = "BOTH sides decaying → option sellers winning, buyers losing to time decay (range / calm day)."
    elif l.ce_chg > 0 > l.pe_chg:
        story = "Calls gaining, puts decaying → the up-move is paying call buyers and put sellers."
    elif l.pe_chg > 0 > l.ce_chg:
        story = "Puts gaining, calls decaying → the down-move is paying put buyers and call sellers."
    else:
        story = "Both sides gaining → IV / big move is beating time decay (buyers' day)."
    lines = [
        f"{n_str} strikes · future {px:+.0f} pts since the first candle.",
        f"CE <b>{l.ce_chg:+,.1f}</b> pts ({ce_p:+.0f}%) · PE <b>{l.pe_chg:+,.1f}</b> pts ({pe_p:+.0f}%) · "
        f"NET <b style='color:{GREEN if net < 0 else RED}'>{net:+,.1f}</b> "
        f"({'sellers of both sides are AHEAD' if net < 0 else 'buyers of both sides are AHEAD'}).",
        f"→ {story}",
        f"Speed, last 30 min: CE {rate_ce:+,.0f} pts/hr · PE {rate_pe:+,.0f} pts/hr"
        + (" — decay accelerating into the close." if expiry_day and l.ts.time() >= time(13, 0) else "."),
    ]
    if expiry_day:
        lines.append(f"<span style='color:{GOLD}'>EXPIRY DAY (0 DTE): out-of-the-money premium goes to ~0 by 15:30 — fast "
                     "decay is expected, not a signal. The side that keeps value is the in-the-money side.</span>")
    return "<br>".join(lines)


HOW = ["<b>CE / PE Change</b> (top) = how many index points the chosen calls / puts have gained or lost since the first "
       "candle — price move + time decay + IV change together. Same scale as StockMojo.",
       "<b>Green above 0 and red below 0</b> while the future rises = calls paying, puts decaying (today's StockMojo picture). "
       "The opposite = a falling market.",
       "<b>Both below 0</b> = time decay is winning: a range day, option SELLERS earn. A short-straddle seller's P&L ≈ −NET.",
       "<b>Gold NET line</b> (CE + PE change): falling = sellers gaining, rising = buyers gaining (a move or IV jump big "
       "enough to beat decay). NET turning up sharply = a breakout is paying buyers — watch price at your level.",
       "<b>Call vs Put Premium</b> (bottom) = the total price of the same calls and puts. The more expensive side is where "
       "the market is paying up; a gold ✕ = the expensive side flipped (usually price crossed the ATM area).",
       "<b>CE + PE (straddle)</b> line = cost of the whole range. Its fall per hour = the day's time decay; a rise = "
       "volatility coming in.",
       "<b>Strikes:</b> ATM ± range is fixed on the LATEST ATM (like StockMojo's 22600–22900) so the same strikes are compared "
       "all day. Fixed = around a strike you choose. Custom = any strikes.",
       "On expiry day decay is fastest after ~13:00 and OTM premium goes to ~0 — read direction from the in-the-money side.",
       "Context, not a trigger — premium-decay readings are not backtested in this app."]


# ═══════════════════════════════════════════════════════════════════════════
# 4 · Streamlit
# ═══════════════════════════════════════════════════════════════════════════
def render(day: date | None = None) -> None:
    import streamlit as st
    import store
    import config

    days = ol._days_available()
    if not days:
        st.info("No option-chain snapshots stored yet.")
        return
    c = st.columns([1.5, 1.5, 2.0, 1.0, 1.8])
    day = c[0].selectbox("Session", days, index=days.index(day) if day in days else 0,
                         format_func=lambda d: d.strftime("%a %d %b %Y"), key="pdec_day")
    tf = c[1].radio("Time", list(TF), index=0, horizontal=True, key="pdec_tf")
    mode = c[2].radio("Strikes", ["ATM ± range", "Fixed", "Custom"], horizontal=True, key="pdec_mode")
    n = c[3].selectbox("± range", [1, 2, 3, 4, 5, 6, 8, 10], index=2, key="pdec_n", disabled=(mode == "Custom"))
    src = c[4].radio("Future line", ["NIFTY future", "Synth future"], horizontal=True, key="pdec_src")

    @st.cache_data(ttl=60, show_spinner=False)
    def _load(d: date, n_files: int):
        snaps = store.load_day(d)
        return snaps, ltp_wide(snaps)

    n_files = len(list((Path(config.SNAPSHOT_DIR) / day.strftime("%Y-%m-%d")).glob("*.meta.json")))
    snaps, (ce, pe, spot, exp) = _load(day, n_files)
    if len(snaps) < 3 or ce.empty:
        st.warning("Not enough snapshots (with option prices) for this session yet.")
        return
    step = float(ol._rules().get("strike_step", 50) or 50)
    last = float(spot.iloc[-1])
    atm0 = float(round(last / step) * step)
    centre, custom = None, []
    if mode == "Fixed":
        centre = st.number_input("Centre strike", value=atm0, step=step, key="pdec_centre")
    elif mode == "Custom":
        opts = sorted(ce.columns, key=lambda k: abs(k - last))[:40]
        custom = st.multiselect("Strikes", sorted(opts), default=[k for k in sorted(opts) if abs(k - atm0) <= 3 * step],
                                key="pdec_custom")
    m = {"ATM ± range": "ATM", "Fixed": "Fixed", "Custom": "Custom"}[mode]
    strikes = pick_strikes(ce, spot, m, int(n), centre, [int(x) for x in custom], step)
    if not strikes:
        st.info("Pick at least one strike.")
        return
    o = st.columns([1, 1, 1, 3])
    show_net = o[0].checkbox("NET (CE + PE)", value=True, key="pdec_net")
    show_str = o[1].checkbox("Straddle line", value=False, key="pdec_str")
    view = o[2].radio("View", ["Chart", "Chart + Table"], horizontal=True, key="pdec_view")

    fut, label, why = future_line(snaps, day, src == "NIFTY future")
    t = build(ce, pe, strikes, tf, fut)
    if t.empty or len(t) < 2:
        st.warning("Those strikes have no prices yet.")
        return
    e = pd.to_datetime(exp, errors="coerce")
    expiry_day = bool(pd.notna(e) and e.date() == day)
    dte = f"{(e.date() - day).days}d" if pd.notna(e) else "?"
    st.caption(f"Expiry {exp} ({dte}) · strikes {min(strikes):,} – {max(strikes):,} ({len(strikes)}) · "
               f"from {t.ts.iloc[0]:%H:%M} · future line: {why}")

    st.plotly_chart(chart_decay(t, label, show_net), use_container_width=True, key="pdec_decay")
    st.plotly_chart(chart_premium(t, label, show_str), use_container_width=True, key="pdec_prem")
    st.markdown(f"<div style='background:{NAVY};border:1px solid {GOLD};border-radius:8px;padding:10px 14px;margin:6px 0'>"
                f"<div style='color:{GOLD};font-weight:700'>PREMIUM DECAY — now</div>"
                f"<div style='color:{WHITE};margin-top:4px;line-height:1.6'>{reads(t, f'{len(strikes)}', expiry_day)}</div></div>",
                unsafe_allow_html=True)
    if view == "Chart + Table":
        tb = pd.DataFrame({"Time": t.ts.dt.strftime("%H:%M"), "Future": t.fut.round(1),
                           "CE premium": t.ce_prem.round(1), "PE premium": t.pe_prem.round(1),
                           "CE change": t.ce_chg.round(1), "PE change": t.pe_chg.round(1),
                           "NET": t.net.round(1), "Straddle": t.straddle.round(1)}).iloc[::-1]
        st.dataframe(tb, use_container_width=True, height=420, hide_index=True)
    li = "".join(f"<li style='margin:3px 0'>{x}</li>" for x in HOW)
    st.markdown(f"<div style='background:{NAVY};border:1px solid {GOLD};border-radius:8px;padding:10px 14px'>"
                f"<div style='color:{GOLD};font-weight:700'>HOW TO READ PREMIUM DECAY</div>"
                f"<ul style='color:{WHITE};margin:6px 0 0 18px'>{li}</ul></div>", unsafe_allow_html=True)
