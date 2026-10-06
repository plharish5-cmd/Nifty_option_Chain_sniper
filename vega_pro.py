"""
vega_pro.py — "Vega Analysis" sub-tab of 🧪 Options Lab, laid out like StockMojo's Vega Analysis and extended.

What the lines are
  Call Vega / Put Vega = premium (in ₹ points per option) that CHANGES IN IV ALONE have added or removed since 09:15,
  for the ATM ± k strikes.  Each minute:  Σ vega × (IV now − IV a minute ago), averaged over the strikes, then added up.
  Doing it minute-by-minute (not "IV now − IV at open") keeps it right when the ATM strike moves and when vega shrinks
  into expiry.
  IV of each call and each put is recomputed from its OWN price (Black-76 on the synth future), so call and put can
  differ — the snapshot's stored IV is often one number for both legs. (Toggle in the panel.)
  Synth Future = ATM strike + call premium − put premium (what the options say NIFTY is worth).
  Put − Call = Put Vega − Call Vega: + = puts gained more from IV (fear), − = calls gained more (upside chase).

Also:  IV panel (ATM call IV, put IV, skew = put − call),  candle table,  plain-English read,  how to use.
Reads stored snapshots only (store.load_day).  Information only — not financial advice.
"""
from __future__ import annotations

from datetime import date, time
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

import options_lab_tab as ol
from options_lab_tab import NAVY, GOLD, WHITE, MUTED, GREEN, RED, BLUE, PURPLE, FUT

TF = {"1m": 1, "3m": 3, "5m": 5, "15m": 15}
MAX_IV_JUMP = 5.0          # an IV move bigger than this between two snapshots is treated as a bad tick and skipped
CLOSE_NOISE = time(15, 25)


# ═══════════════════════════════════════════════════════════════════════════
# 1 · Data
# ═══════════════════════════════════════════════════════════════════════════
# Black-76 on the synth future (r = 0): each leg's IV from ITS OWN price, like StockMojo
def _ncdf(x):
    x = np.asarray(x, dtype=float)
    z = np.abs(x) / np.sqrt(2.0)
    t = 1.0 / (1.0 + 0.3275911 * z)
    erf = 1.0 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * np.exp(-z * z)
    return 0.5 * (1.0 + np.sign(x) * erf)


def _npdf(x):
    return np.exp(-0.5 * np.asarray(x, dtype=float) ** 2) / np.sqrt(2 * np.pi)


def _b76(F, K, T, sig, call):
    sT = sig * np.sqrt(T)
    d1 = (np.log(F / K) + 0.5 * sT * sT) / sT
    d2 = d1 - sT
    c = F * _ncdf(d1) - K * _ncdf(d2)
    return np.where(call, c, c - F + K), d1


def implied(F, K, T, price, call):
    """Vectorised bisection. Returns (IV in %, vega in ₹ per 1 IV point); NaN where the price is below intrinsic."""
    K, price, call = (np.asarray(x, dtype=float) for x in (K, price, call))
    call = call.astype(bool)
    intrinsic = np.where(call, np.maximum(F - K, 0), np.maximum(K - F, 0))
    ok = np.isfinite(price) & (price > intrinsic + 0.05) & (T > 0)
    lo, hi = np.full(K.shape, 0.005), np.full(K.shape, 3.0)
    for _ in range(50):
        mid = 0.5 * (lo + hi)
        pm, _ = _b76(F, K, T, mid, call)
        hi = np.where(pm > price, mid, hi)
        lo = np.where(pm > price, lo, mid)
    sig = 0.5 * (lo + hi)
    _, d1 = _b76(F, K, T, sig, call)
    vega = F * np.sqrt(T) * _npdf(d1) / 100.0
    return np.where(ok, sig * 100, np.nan), np.where(ok, vega, np.nan)


def _years_left(ts, expiry) -> float:
    e = pd.to_datetime(expiry, errors="coerce")
    if pd.isna(e):
        return np.nan
    mins = (pd.Timestamp(e.date()) + pd.Timedelta(hours=15, minutes=30) - pd.Timestamp(ts)).total_seconds() / 60
    return max(mins, 1.0) / (365 * 1440)


def series(snaps: list, k: int, centre: float | None = None, step: float = 50, own_iv: bool = True) -> pd.DataFrame:
    """One row per snapshot: spot, synth future, ATM IVs, and the cumulative IV-only premium change per side.
    own_iv=True → IV & vega of every leg recomputed from its own LTP (Black-76 on the synth future);
    False → the IV / vega columns stored in the snapshot."""
    rows, prev = [], {}
    cum_c = cum_p = 0.0
    for ts, spot, df in snaps:
        if df is None or df.empty:
            continue
        d = ol._one_expiry(df).copy()
        for c in ("strike", "ce_ltp", "pe_ltp", "ce_iv", "pe_iv", "ce_vega", "pe_vega"):
            if c in d:
                d[c] = pd.to_numeric(d[c], errors="coerce")
        d = d.sort_values("strike").reset_index(drop=True)
        i_atm = (d["strike"] - spot).abs().values.argmin()
        a0 = d.iloc[i_atm]
        synth = float(a0["strike"] + a0["ce_ltp"] - a0["pe_ltp"]) if {"ce_ltp", "pe_ltp"} <= set(d.columns) \
            and pd.notna(a0.get("ce_ltp")) and pd.notna(a0.get("pe_ltp")) else np.nan
        if not np.isfinite(synth) or abs(synth - spot) > 400:
            synth = float(spot)
        if own_iv:
            if not {"ce_ltp", "pe_ltp", "expiry"} <= set(d.columns):
                continue
            T = _years_left(ts, d["expiry"].iloc[0])
            if not np.isfinite(T):
                continue
            K = d["strike"].values
            d["ce_iv"], d["ce_vega"] = implied(synth, K, T, d["ce_ltp"].values, np.ones(len(d)))
            d["pe_iv"], d["pe_vega"] = implied(synth, K, T, d["pe_ltp"].values, np.zeros(len(d)))
        elif not {"ce_vega", "pe_vega", "ce_iv", "pe_iv"} <= set(d.columns):
            continue
        atm_k = float(centre) if centre else float(a0["strike"])
        near = d[(d["strike"] >= atm_k - k * step) & (d["strike"] <= atm_k + k * step)]
        dc = dp = 0.0
        nc = npt = 0
        for r in near.itertuples():
            p = prev.get(r.strike)
            if p is None:
                continue
            if not any(pd.isna(v) for v in (r.ce_vega, r.ce_iv, p[0], p[2])) and abs(r.ce_iv - p[2]) <= MAX_IV_JUMP:
                dc += 0.5 * (r.ce_vega + p[0]) * (r.ce_iv - p[2]); nc += 1
            if not any(pd.isna(v) for v in (r.pe_vega, r.pe_iv, p[1], p[3])) and abs(r.pe_iv - p[3]) <= MAX_IV_JUMP:
                dp += 0.5 * (r.pe_vega + p[1]) * (r.pe_iv - p[3]); npt += 1
        for r in d.itertuples():
            prev[r.strike] = (r.ce_vega, r.pe_vega, r.ce_iv, r.pe_iv)
        cum_c += dc / nc if nc else 0.0
        cum_p += dp / npt if npt else 0.0
        # IV panel: OTM legs only (calls above, puts below the synth future) — at the ATM strike call IV = put IV by parity
        oc = near[near["strike"] >= synth]["ce_iv"].dropna()
        op = near[near["strike"] <= synth]["pe_iv"].dropna()
        rows.append(dict(ts=pd.Timestamp(ts), spot=float(spot), synth=synth, atm=atm_k,
                         ce_iv=float(oc.mean()) if len(oc) else np.nan, pe_iv=float(op.mean()) if len(op) else np.nan,
                         call_vega=cum_c, put_vega=cum_p))
    s = pd.DataFrame(rows)
    if not s.empty:
        s["diff"] = s["put_vega"] - s["call_vega"]
        s["skew"] = s["pe_iv"] - s["ce_iv"]
    return s


def to_candles(s: pd.DataFrame, minutes: int) -> pd.DataFrame:
    if minutes <= 1 or s.empty:
        return s.copy()
    x = s.set_index("ts").resample(f"{minutes}min", origin="start_day", offset="15min").last()
    return x.dropna(subset=["spot"]).reset_index()


# ═══════════════════════════════════════════════════════════════════════════
# 2 · Charts
# ═══════════════════════════════════════════════════════════════════════════
def chart(s: pd.DataFrame, show_diff: bool, title: str) -> go.Figure:
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.68, 0.32], vertical_spacing=0.06,
                        specs=[[{"secondary_y": True}], [{"secondary_y": True}]],
                        subplot_titles=("Synth Future · Call Vega · Put Vega (premium from IV alone, since open)",
                                        "OTM IV — calls above · puts below · skew (put − call)"))
    fig.add_trace(go.Scatter(x=s["ts"], y=s["synth"], name="Synth Future", line=dict(color=FUT, dash="dot", width=1.4),
                             hovertemplate="Synth Future %{y:,.1f}<extra></extra>"), 1, 1, secondary_y=False)
    fig.add_trace(go.Scatter(x=s["ts"], y=s["call_vega"], name="Call Vega", fill="tozeroy",
                             line=dict(color=GREEN, width=2.2), fillcolor="rgba(34,197,94,0.15)",
                             hovertemplate="Call Vega %{y:+.2f} pts<extra></extra>"), 1, 1, secondary_y=True)
    fig.add_trace(go.Scatter(x=s["ts"], y=s["put_vega"], name="Put Vega", fill="tozeroy",
                             line=dict(color=RED, width=2.2), fillcolor="rgba(239,68,68,0.15)",
                             hovertemplate="Put Vega %{y:+.2f} pts<extra></extra>"), 1, 1, secondary_y=True)
    if show_diff:
        fig.add_trace(go.Scatter(x=s["ts"], y=s["diff"], name="Put − Call", line=dict(color=PURPLE, width=1.8, dash="dash"),
                                 hovertemplate="Put − Call %{y:+.2f} pts<extra></extra>"), 1, 1, secondary_y=True)
    fig.add_hline(y=0, line=dict(color=MUTED, width=1, dash="dot"), row=1, col=1, secondary_y=True)

    fig.add_trace(go.Scatter(x=s["ts"], y=s["ce_iv"], name="Call IV (OTM)", line=dict(color=GREEN, width=1.6),
                             hovertemplate="Call IV %{y:.2f}%<extra></extra>"), 2, 1, secondary_y=False)
    fig.add_trace(go.Scatter(x=s["ts"], y=s["pe_iv"], name="Put IV (OTM)", line=dict(color=RED, width=1.6),
                             hovertemplate="Put IV %{y:.2f}%<extra></extra>"), 2, 1, secondary_y=False)
    fig.add_trace(go.Bar(x=s["ts"], y=s["skew"], name="Skew (put − call IV)", opacity=0.35,
                         marker_color=[RED if v > 0 else GREEN for v in s["skew"].fillna(0)],
                         hovertemplate="Skew %{y:+.2f}<extra></extra>"), 2, 1, secondary_y=True)

    fig.update_yaxes(title_text="Synth Future", row=1, col=1, secondary_y=False, tickformat=",.0f")
    fig.update_yaxes(title_text="pts from IV", row=1, col=1, secondary_y=True, showgrid=False, zeroline=False)
    fig.update_yaxes(title_text="IV %", row=2, col=1, secondary_y=False)
    fig.update_yaxes(title_text="skew", row=2, col=1, secondary_y=True, showgrid=False)
    ol._layout(fig, title, h=780)
    for a in fig.layout.annotations:
        a.font = dict(color=GOLD, size=13)
    fig.update_layout(legend=dict(orientation="h", x=0, xanchor="left", y=-0.06, yanchor="top"),
                      margin=dict(l=70, r=60, t=60, b=70), hovermode="x unified")
    return fig


# ═══════════════════════════════════════════════════════════════════════════
# 3 · Reads
# ═══════════════════════════════════════════════════════════════════════════
def regime(px: float, cv: float, pv: float, eps: float) -> tuple[str, str]:
    """Price move + which side's IV moved → plain meaning (label, colour)."""
    up, dn = px > 0, px < 0
    pu, pd_, cu = pv > eps, pv < -eps, cv > eps
    if dn and pu:
        return "Panic — puts bid up as price falls (fear rising)", RED
    if pv < -eps and cv < -eps and cv <= pv:
        return "IV crush — both sides losing premium (calls more), option buyers bleed", MUTED
    if up and pd_:
        return "Fear leaving — put IV melting as price rises (healthy rally)", GREEN
    if up and cu:
        return "Upside chase — calls bid up as price rises (can overshoot)", GOLD
    if dn and pd_ and cv < -eps:
        return "Calm drift down — IV crush both sides, sellers in control", MUTED
    if pv < -eps and cv < -eps:
        return "IV crush — both sides losing premium, option buyers bleed", MUTED
    if pu and cu:
        return "IV expanding both sides — big move expected", GOLD
    return "No clear IV story", MUTED


def table(c: pd.DataFrame) -> pd.DataFrame:
    t = c.copy()
    t["px"] = t["synth"].diff()
    t["dc"], t["dp"] = t["call_vega"].diff(), t["put_vega"].diff()
    eps = max(0.02, float(t[["dc", "dp"]].abs().stack().quantile(0.4))) if len(t) > 3 else 0.02
    t["read"] = [regime(a, b, d, eps)[0].split(" — ")[0] if pd.notna(a) else "—"
                 for a, b, d in zip(t["px"], t["dc"], t["dp"])]
    out = pd.DataFrame({"Time": t["ts"].dt.strftime("%H:%M"), "Synth Fut": t["synth"].round(1),
                        "Call IV": t["ce_iv"].round(2), "Put IV": t["pe_iv"].round(2), "Skew": t["skew"].round(2),
                        "Call Vega": t["call_vega"].round(2), "Put Vega": t["put_vega"].round(2),
                        "Put − Call": t["diff"].round(2), "Read": t["read"]})
    return out.iloc[::-1].reset_index(drop=True)


def summary(s: pd.DataFrame, expiry_day: bool) -> str:
    l, f = s.iloc[-1], s.iloc[0]
    px = float(l.synth - f.synth)
    lab, col = regime(px, float(l.call_vega), float(l.put_vega), 0.05)
    tail = s.tail(15)
    lab2, col2 = regime(float(tail.synth.iloc[-1] - tail.synth.iloc[0]),
                        float(tail.call_vega.iloc[-1] - tail.call_vega.iloc[0]),
                        float(tail.put_vega.iloc[-1] - tail.put_vega.iloc[0]), 0.03)
    lines = [
        f"Since open: NIFTY (synth) <b>{px:+.0f}</b> pts · Call Vega <b>{l.call_vega:+.2f}</b> · "
        f"Put Vega <b>{l.put_vega:+.2f}</b> · Put − Call <b>{l.put_vega - l.call_vega:+.2f}</b> pts per option",
        f"OTM IV call {f.ce_iv:.1f}% → {l.ce_iv:.1f}% · put {f.pe_iv:.1f}% → {l.pe_iv:.1f}% · skew now {l['skew']:+.2f}",
        f"Whole day: <span style='color:{col};font-weight:700'>{lab}</span>",
        f"Last ~15 snapshots: <span style='color:{col2};font-weight:700'>{lab2}</span>",
    ]
    if expiry_day:
        lines.append(f"<span style='color:{GOLD}'>EXPIRY DAY: vega is tiny and IV falls all day by design — a steadily "
                     "falling Put/Call Vega is normal decay, not a signal. Watch only sudden jumps.</span>")
    return "<br>".join(lines)


HOW = ["<b>What the lines are:</b> how many ₹ points of premium IV changes alone have added (+) or removed (−) from the "
       "ATM call and put since 09:15. Price moves are taken out — this is pure fear/greed in option pricing.",
       "<b>Red (Put Vega) rising while price falls</b> = traders paying up for protection → panic. When it stops rising "
       "and turns down while price is still falling, the fall often slows (sellers are no longer scared).",
       "<b>Red falling while price rises</b> = fear leaving → the healthiest kind of rally (like StockMojo's chart today: "
       "price up, put vega sliding all day).",
       "<b>Green (Call Vega) rising with price</b> = traders chasing upside with calls → strong but can overshoot; a "
       "sudden drop in call vega after a spike = the chase is over.",
       "<b>Both below 0 and falling</b> = IV crush → option BUYERS lose even when price goes their way; favour selling "
       "premium / smaller buyer targets.",
       "<b>Put − Call (purple)</b> above 0 = puts gained more from IV (market more scared of a fall); below 0 = calls "
       "gained more. A turn in this line often comes before a turn in price — but test it on your own data first.",
       "<b>Skew bars</b> (bottom): put IV above call IV is normal for NIFTY; a fast widening = fresh fear, narrowing = relief.",
       "<b>Synth Future</b> (dotted) = strike + call − put: what the options price NIFTY at. A gap from spot = futures premium.",
       "Ignore the last 5 minutes (15:25–15:30): closing ticks make IVs jump. The default hides them.",
       "Context, not a trigger — vega/IV readings are not backtested in this app yet. Use with price at your levels."]


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
    c = st.columns([1.5, 1.6, 1.4, 1.0, 1.6, 1.7])
    day = c[0].selectbox("Session", days, index=days.index(day) if day in days else 0,
                         format_func=lambda d: d.strftime("%a %d %b %Y"), key="vg_day")
    tf = c[1].radio("Timeframe", list(TF), index=0, horizontal=True, key="vg_tf")
    mode = c[2].radio("Strike", ["Auto", "Fixed"], horizontal=True, key="vg_mode",
                      help="Auto = ATM re-centres every minute. Fixed = the strikes around the centre you choose.")
    k = c[3].selectbox("ATM ±", [1, 2, 3, 5], index=1, key="vg_k")
    view = c[4].radio("View", ["Chart", "Chart + Table"], horizontal=True, key="vg_view")
    opts = c[5]
    show_diff = opts.checkbox("Show Put − Call", value=True, key="vg_diff")
    hide_close = opts.checkbox("Hide 15:25–15:30", value=True, key="vg_close")
    own_iv = opts.checkbox("IV from each option's own price", value=True, key="vg_own",
                           help="On (like StockMojo): call and put IV computed separately from their LTPs. "
                                "Off: the IV stored in your snapshots (often the same number for call and put).")

    @st.cache_data(ttl=60, show_spinner=False)
    def _snaps(d: date, n_files: int):
        return store.load_day(d)

    n_files = len(list((Path(config.SNAPSHOT_DIR) / day.strftime("%Y-%m-%d")).glob("*.meta.json")))
    snaps = _snaps(day, n_files)
    if len(snaps) < 3:
        st.warning("Not enough snapshots for this session yet.")
        return
    step = float(ol._rules().get("strike_step", 50) or 50)
    centre = None
    if mode == "Fixed":
        spot = float(snaps[-1][1])
        centre = st.number_input("Centre strike", value=float(round(spot / step) * step), step=step, key="vg_centre")
    if hide_close:
        snaps = [x for x in snaps if pd.Timestamp(x[0]).time() < CLOSE_NOISE]
    s = series(snaps, int(k), centre, step, own_iv)
    if s.empty or len(s) < 3:
        st.warning("These snapshots have no vega / IV columns.")
        return
    cd = to_candles(s, TF[tf])
    expiry_day = False
    try:
        exp = pd.to_datetime(ol._one_expiry(snaps[-1][2])["expiry"].iloc[0], errors="coerce")
        expiry_day = bool(pd.notna(exp) and exp.date() == day)
    except Exception:                                           # noqa: BLE001
        pass
    st.caption(f"{len(s)} snapshots · {s.ts.iloc[0]:%H:%M}–{s.ts.iloc[-1]:%H:%M} · strikes ATM ±{k} "
               f"({'re-centred each minute' if mode == 'Auto' else f'around {centre:,.0f}'}) · "
               f"{'EXPIRY DAY (0 DTE)' if expiry_day else 'not expiry day'}")

    st.plotly_chart(chart(cd, show_diff, f"Vega Analysis ({tf})"), use_container_width=True, key="vg_chart")
    st.markdown(f"<div style='background:{NAVY};border:1px solid {GOLD};border-radius:8px;padding:10px 14px;margin:6px 0'>"
                f"<div style='color:{GOLD};font-weight:700'>VEGA — today</div>"
                f"<div style='color:{WHITE};margin-top:4px;line-height:1.6'>{summary(s, expiry_day)}</div></div>",
                unsafe_allow_html=True)
    if view == "Chart + Table":
        st.dataframe(table(cd), use_container_width=True, height=420, hide_index=True)
    li = "".join(f"<li style='margin:3px 0'>{x}</li>" for x in HOW)
    st.markdown(f"<div style='background:{NAVY};border:1px solid {GOLD};border-radius:8px;padding:10px 14px'>"
                f"<div style='color:{GOLD};font-weight:700'>HOW TO READ VEGA ANALYSIS</div>"
                f"<ul style='color:{WHITE};margin:6px 0 0 18px'>{li}</ul></div>", unsafe_allow_html=True)
