"""
strike_advisor_pro.py — "🧭 Strike Advisor" sub-tab of 🧪 Options Lab.

One page that pulls together everything the other Options Lab tabs measure and answers two questions:

  1 · HOW FAR CAN NIFTY GO TODAY?   (move map from today's open)
      open / high / low / now · expected 1-day move from the OPENING straddle (σ-day) and bands at ±1, ±1.5, ±2 σ ·
      how much of a normal day's range (ATR-14) is already used · what is left for the rest of today (straddle, √time) ·
      the levels in the way: put / call walls, max pain, gamma flip / pin, previous close, open.
  2 · WHICH STRIKE, CE OR PE, RIGHT NOW?
      side bias from the writers' flow, walls, rolls, day type, gamma and IV (the same filter as the ★ PRO brief),
      then for every strike near the money a Greeks scenario (Taylor):
          premium change ≈ Δ·move + ½·Γ·move² + θ·(minutes/375) + ν·ΔIV
      for (a) the move to the first level in the way and (b) your stop (40 pts against you by default).
      → reward ₹, risk ₹, reward:risk, theta cost, IV skew, spread, OI · BEST COURSE PICK (MJ: ITM 100–200, ×100,
      premium ≈ ₹200) and the FAST / aggressive pick, each with premium target and premium stop.

Information only — not financial advice. Scenario maths is a first-order Greeks approximation (it assumes the Greeks
stay as they are for the move); none of these reads are backtested in this app.
"""
from __future__ import annotations

import math
from datetime import date, time
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go

import options_lab_tab as ol
from options_lab_tab import NAVY, GOLD, WHITE, MUTED, GREEN, RED, FUT

ORANGE, BLUE, PURPLE = "#f97316", "#38bdf8", "#a78bfa"


def _fin(v) -> bool:
    try:
        return v is not None and math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def _lot() -> int:
    try:
        import config
        return int(getattr(config, "LOT_SIZE", 65) or 65)
    except Exception:                                           # noqa: BLE001
        return 65


# ═══════════════════════════════════════════════════════════════════════════
# 1 · Move map
# ═══════════════════════════════════════════════════════════════════════════
def _daily(day: date) -> dict:
    """ATR-14 and previous close from your daily OHLC (data/ohlc)."""
    out = dict(atr=np.nan, avg_rng=np.nan, prev_close=np.nan)
    try:
        import levels
        d = levels.load_ohlc()
        d = d[pd.to_datetime(d["Date"]).dt.date < day].tail(20)
        if len(d) < 5:
            return out
        h, l, c = d["High"].astype(float), d["Low"].astype(float), d["Close"].astype(float)
        tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
        out.update(atr=float(tr.tail(14).mean()), avg_rng=float((h - l).tail(10).mean()), prev_close=float(c.iloc[-1]))
    except Exception:                                           # noqa: BLE001
        pass
    return out


def move_map(a: dict, day: date) -> dict:
    import olab_pro as op
    p = a["p"]
    spots = p.groupby("ts")["spot"].first().sort_index()
    t0 = spots.index[0]
    o, now, hi, lo = float(spots.iloc[0]), float(spots.iloc[-1]), float(spots.max()), float(spots.min())
    c0 = op.at(p, t0)
    s0 = op.straddle(c0, o)
    m_exp0 = op.minutes_to_expiry(t0, a["expiry"])
    sig = np.nan
    if s0 and m_exp0 and np.isfinite(m_exp0) and m_exp0 > 0:
        # ATM straddle ≈ 0.8 σ of the move to expiry → σ for one session = 1.25 × straddle × √(375 / minutes to expiry)
        sig = 1.25 * s0[0] * math.sqrt(min(375.0, m_exp0) / m_exp0)
    d = _daily(day)
    em = a.get("em") or {}
    left = float(em.get("today_left", np.nan)) if em else np.nan
    lv = []                                                     # (name, price, colour)
    w = a.get("walls") or {}
    if w.get("floor"):
        lv.append(("Put wall", float(w["floor"]["strike"]), GREEN))
    if w.get("ceiling"):
        lv.append(("Call wall", float(w["ceiling"]["strike"]), RED))
    if a.get("mp_now"):
        lv.append(("Max pain", float(a["mp_now"]), ORANGE))
    g = a.get("gamma") or {}
    if g.get("flip"):
        lv.append(("Gamma flip", float(g["flip"]), PURPLE))
    if g.get("pin"):
        lv.append(("Gamma pin", float(g["pin"]), BLUE))
    if np.isfinite(d["prev_close"]):
        lv.append(("Prev close", d["prev_close"], MUTED))
    lv.append(("Open", o, GOLD))
    return dict(open=o, now=now, hi=hi, lo=lo, move=now - o, pct=100 * (now - o) / o, sig=sig, s0=s0[0] if s0 else np.nan,
                n_sig=(now - o) / sig if np.isfinite(sig) and sig else np.nan, rng=hi - lo,
                atr=d["atr"], atr_used=(hi - lo) / d["atr"] if np.isfinite(d["atr"]) and d["atr"] else np.nan,
                prev_close=d["prev_close"], left=left, levels=lv, spots=spots)


def path_levels(mm: dict, spot: float, side: str, reach: float) -> tuple[float, str, list]:
    """First level in the trade's direction (CE = up, PE = down) within reach; else spot ± reach."""
    up = side == "CE"
    cand = sorted([(n, v) for n, v, _ in mm["levels"] if (v > spot + 5 if up else v < spot - 5)],
                  key=lambda x: (x[1] - spot) * (1 if up else -1))
    inside = [x for x in cand if abs(x[1] - spot) <= reach]
    if inside:
        return inside[0][1], inside[0][0], cand
    return spot + (reach if up else -reach), "rest-of-day expected move", cand


# ═══════════════════════════════════════════════════════════════════════════
# 2 · Strike scenarios
# ═══════════════════════════════════════════════════════════════════════════
def chain_now(df: pd.DataFrame) -> pd.DataFrame:
    d = ol._one_expiry(df).copy()
    for c_ in d.columns:
        if c_ != "expiry":
            d[c_] = pd.to_numeric(d[c_], errors="coerce")
    return d.dropna(subset=["strike"]).sort_values("strike")


def scenarios(ch: pd.DataFrame, spot: float, side: str, move: float, sl: float, mins: float,
              div_win: float, div_loss: float, window: float = 300) -> pd.DataFrame:
    p = side.lower()
    x = ch[(ch.strike - spot).abs() <= window].copy()
    g = lambda c: x[f"{p}_{c}"] if f"{p}_{c}" in x else pd.Series(np.nan, index=x.index)
    out = pd.DataFrame({"strike": x.strike.astype(int), "ltp": g("ltp"), "bid": g("bid"), "ask": g("ask"),
                        "delta": g("delta"), "gamma": g("gamma"), "theta": g("theta"), "vega": g("vega"),
                        "iv": g("iv"), "oi": g("oi")})
    out = out[(out.ltp > 1) & out.delta.notna()]
    if out.empty:
        return out
    sgn = 1 if side == "CE" else -1
    out["itm"] = (spot - out.strike) * sgn
    D, G, T, V = out.delta.abs(), out.gamma.fillna(0), out.theta.fillna(0), out.vega.fillna(0)
    tcost = T * mins / 375.0                                     # θ is per day → share of a 375-min session (negative)
    out["win"] = D * move + 0.5 * G * move ** 2 + tcost + V * div_win
    out["loss"] = -D * sl + 0.5 * G * sl ** 2 + tcost + V * div_loss          # usually negative
    out["rr"] = out.win / out.loss.abs().replace(0, np.nan)
    out["ret_pct"] = 100 * out.win / out.ltp
    out["theta_pct_day"] = 100 * T.abs() / out.ltp
    atm_iv = float(out.loc[(out.strike - spot).abs().idxmin(), "iv"]) if out.iv.notna().any() else np.nan
    out["ivskew"] = out.iv - atm_iv
    out["spread"] = 100 * (out.ask - out.bid) / out.ltp
    out["course"] = (out.strike % 100 == 0) & out.itm.between(50, 250) & out.ltp.between(120, 320)
    out["kind"] = np.where(out.itm > 25, "ITM", np.where(out.itm < -25, "OTM", "ATM"))
    out["prem_tgt"] = out.ltp + out.win
    out["prem_sl"] = out.ltp + out.loss
    return out.sort_values("strike")


def pick(sc: pd.DataFrame) -> tuple[pd.Series | None, pd.Series | None]:
    if sc is None or sc.empty:
        return None, None
    ok = sc[(sc.spread.fillna(0) <= 3) & (sc.win > 0)].copy()
    # course pick: reward:risk, weighted toward MJ's ≈ ₹200 premium (deeper ITM always scores a little more R:R)
    ok["fit"] = ok.rr * (1 - (ok.ltp - 200).abs() / 400).clip(0.2, 1)
    course = ok[ok.course]
    itm = ok[ok.kind == "ITM"]
    best = (course.sort_values(["fit", "oi"], ascending=False).iloc[0] if len(course) else
            itm.sort_values("fit", ascending=False).iloc[0] if len(itm) else None)
    # fast pick: ATM or 1–2 strikes OTM only (far OTM = lottery tickets)
    fast = ok[(ok.itm <= 25) & (ok.itm >= -100)].sort_values("ret_pct", ascending=False)
    return best, (fast.iloc[0] if len(fast) else None)


# ═══════════════════════════════════════════════════════════════════════════
# 3 · Charts / cards
# ═══════════════════════════════════════════════════════════════════════════
def chart_map(mm: dict) -> go.Figure:
    s = mm["spots"]
    fig = go.Figure()
    o, sig = mm["open"], mm["sig"]
    if np.isfinite(sig):
        for k, a_ in ((2, 0.05), (1.5, 0.07), (1, 0.10)):
            fig.add_hrect(y0=o - k * sig, y1=o + k * sig, fillcolor=f"rgba(212,175,55,{a_})", line_width=0)
            for sg in (1, -1):
                fig.add_annotation(x=1.0, xref="paper", y=o + sg * k * sig, xanchor="left", showarrow=False,
                                   text=f"{'+' if sg > 0 else '−'}{k:g}σ {o + sg * k * sig:,.0f}",
                                   font=dict(color=GOLD, size=10))
    for i, (n, v, col) in enumerate(sorted(mm["levels"], key=lambda z: z[1])):
        fig.add_hline(y=v, line=dict(color=col, dash="dot", width=1))
        fig.add_annotation(x=0.0 + 0.16 * (i % 3), xref="paper", y=v, xanchor="left", showarrow=False, yshift=7,
                           text=f"{n} {v:,.0f}", font=dict(color=col, size=10))
    fig.add_trace(go.Scatter(x=s.index, y=s.values, name="NIFTY", line=dict(color=WHITE, width=1.6),
                             hovertemplate="%{x|%H:%M} NIFTY %{y:,.1f}<extra></extra>"))
    ol._layout(fig, "Move map — today from the open (bands = expected move priced at the open)", h=430)
    fig.update_layout(margin=dict(l=10, r=95, t=50, b=10), showlegend=False)
    fig.update_yaxes(tickformat=",.0f")
    return fig


def _card(title: str, body: str, border: str = GOLD) -> str:
    return (f"<div style='background:{NAVY};border:1px solid {border};border-radius:8px;padding:10px 14px;margin:6px 0'>"
            f"<div style='color:{GOLD};font-weight:700'>{title}</div>"
            f"<div style='color:{WHITE};margin-top:4px;line-height:1.6'>{body}</div></div>")


def pick_html(side: str, r: pd.Series | None, label: str, spot: float, sl: float, tgt_lvl: float, lot: int,
              grade: str) -> str:
    col = GREEN if side == "CE" else RED
    if r is None:
        return _card(f"{label} — {side}", "No strike passes (liquidity / premium).", border=col)
    k = "ITM" if r.itm > 25 else "OTM" if r.itm < -25 else "ATM"
    nifty_sl = spot - sl if side == "CE" else spot + sl
    body = (f"<span style='font-size:1.25rem;font-weight:800;color:{col}'>{int(r.strike)} {side}</span> "
            f"<span style='color:{MUTED}'>{k} {abs(r.itm):,.0f} pts · {grade}</span><br>"
            f"Premium <b>{r.ltp:,.2f}</b> (₹{r.ltp * lot:,.0f}/lot) · Δ {r.delta:+.2f} · Γ {r.gamma:.4f} · "
            f"θ {r.theta:,.1f}/day ({r.theta_pct_day:.0f}%) · ν {r.vega:.2f} · IV {r.iv:.1f}"
            f"{f' ({float(r.ivskew):+.1f} vs ATM)' if _fin(r.ivskew) else ''}<br>"
            f"🎯 NIFTY to <b>{tgt_lvl:,.0f}</b> → premium ≈ <b style='color:{GREEN}'>{r.prem_tgt:,.1f}</b> "
            f"(+{r.win:,.1f} pts = +₹{r.win * lot:,.0f})<br>"
            f"🛑 NIFTY to <b>{nifty_sl:,.0f}</b> ({sl:.0f} pts against) → premium ≈ <b style='color:{RED}'>{r.prem_sl:,.1f}</b> "
            f"({r.loss:,.1f} pts = −₹{abs(r.loss) * lot:,.0f})<br>"
            f"Reward : risk <b>{r.rr:,.2f}</b> · return on premium {r.ret_pct:+.0f}% · spread {r.spread:.1f}%")
    return _card(f"{label} — {side}", body, border=col)


HOW = [
    "<b>σ-day</b> = the one-day move the OPENING ATM straddle priced (straddle ≈ 0.8 σ to expiry, scaled to one session). "
    "About 2 days in 3 stay inside ±1σ of the open; beyond ±1.5σ is a big day; ±2σ is rare.",
    "<b>Move so far in σ</b>: past ~1.5σ, a NEW entry in the same direction has poor reward:risk — the easy part is done; "
    "wait for a pull-back or a fresh setup.",
    "<b>ATR used</b> = today's high–low ÷ the average day (ATR-14 from your daily OHLC). Above ~100% the day is already "
    "larger than normal.",
    "<b>Rest-of-day move</b> = the straddle scaled by √(minutes left) — how far price is still priced to travel today.",
    "<b>Levels in the way</b>: put wall (support), call wall (resistance), max pain, gamma flip (above = dealers dampen, "
    "below = dealers chase), gamma pin, previous close, open. The target is the FIRST level in your direction that is "
    "inside the rest-of-day move.",
    "<b>Strike scenario</b> = Δ·move + ½Γ·move² + θ·time + ν·ΔIV. ITM = more Δ (moves like NIFTY), less θ % and ν per ₹. "
    "ATM/OTM = more % return on a fast move but bleeds faster if it stalls. After a big fall, PUT IV is pumped — if the "
    "fall pauses IV drops and puts lose even when flat (set ΔIV negative to see it).",
    "<b>Best course pick</b> = MJ's rule (ITM 100–200, multiples of 100, premium ≈ ₹200) with the best reward:risk. "
    "<b>Fast pick</b> = best % return if the move comes quickly — smaller size, tighter discipline.",
    "The side grade (✅ / ⚠ / ⛔) is the same writer-flow filter as the ★ PRO brief. Never take a ⛔ side.",
]


# ═══════════════════════════════════════════════════════════════════════════
# 4 · Streamlit
# ═══════════════════════════════════════════════════════════════════════════
def render(day: date | None = None) -> None:
    import streamlit as st
    import olab_pro as op

    days = ol._days_available()
    if not days:
        st.info("No option-chain snapshots stored yet.")
        return
    c = st.columns([1.6, 1.3, 1.3, 1.6, 2.2])
    day = c[0].selectbox("Session", days, index=days.index(day) if day in days else 0,
                         format_func=lambda d: d.strftime("%a %d %b %Y"), key="sad_day")
    sl = float(c[1].number_input("Stop (NIFTY pts)", 10.0, 150.0, 40.0, 5.0, key="sad_sl",
                                 help="MJ's fixed stop: 40 pts against you"))
    mins = float(c[2].selectbox("Hold (min)", [15, 30, 45, 60, 90], index=1, key="sad_mins",
                                help="How long you expect to hold — sets the theta cost"))
    tmode = c[3].radio("Target", ["First level", "Fixed pts"], horizontal=True, key="sad_tmode")
    tfix = float(c[4].slider("Fixed target (pts)", 10, 150, 40, 5, key="sad_tfix", disabled=tmode != "Fixed pts"))
    c2 = st.columns([2, 2, 3])
    div_win = float(c2[0].slider("IV change if it WORKS (pts)", -3.0, 3.0, 0.0, 0.5, key="sad_divw",
                                 help="A continued fall often lifts put IV; a rally usually lowers IV"))
    div_loss = float(c2[1].slider("IV change if STOPPED (pts)", -3.0, 3.0, -0.5, 0.5, key="sad_divl",
                                  help="A failed trade usually comes with IV easing"))

    with st.spinner("Reading the session…"):
        snaps, a = op.load(day)
    if not a or not snaps:
        st.warning("Not enough snapshots for this session yet.")
        return
    lot = _lot()
    mm = move_map(a, day)
    spot = float(a["spot"])
    ch = chain_now(snaps[-1][2])

    # ── 1 · how far today ───────────────────────────────────────────────────────────────────────────
    k = st.columns(6)
    k[0].metric("Open", f"{mm['open']:,.1f}")
    k[1].metric("Now", f"{mm['now']:,.1f}", f"{mm['move']:+,.0f} ({mm['pct']:+.2f}%)")
    k[2].metric("Day range", f"{mm['rng']:,.0f} pts", f"H {mm['hi']:,.0f} · L {mm['lo']:,.0f}", delta_color="off")
    k[3].metric("σ-day (open straddle)", f"{mm['sig']:,.0f}" if np.isfinite(mm["sig"]) else "—",
                f"move = {mm['n_sig']:+.2f} σ" if np.isfinite(mm["n_sig"]) else None, delta_color="off")
    k[4].metric("ATR-14 used", f"{mm['atr_used'] * 100:,.0f}%" if np.isfinite(mm["atr_used"]) else "—",
                f"ATR {mm['atr']:,.0f}" if np.isfinite(mm["atr"]) else None, delta_color="off")
    k[5].metric("Rest of today", f"±{mm['left']:,.0f}" if np.isfinite(mm["left"]) else "—", "priced move left",
                delta_color="off")
    st.plotly_chart(chart_map(mm), use_container_width=True, key="sad_map")

    ns, sig = mm["n_sig"], mm["sig"]
    dirw = "fallen" if mm["move"] < 0 else "risen"
    L = [f"NIFTY has <b style='color:{RED if mm['move'] < 0 else GREEN}'>{dirw} {abs(mm['move']):,.0f} pts "
         f"({mm['pct']:+.2f}%)</b> from the open {mm['open']:,.1f}; today's range is {mm['rng']:,.0f} pts."]
    if np.isfinite(sig):
        band = ("inside ±1σ — a normal day so far" if abs(ns) < 1 else "between 1σ and 1.5σ — a big day" if abs(ns) < 1.5
                else "beyond 1.5σ — an outsized day; fresh entries in the same direction have poor odds" if abs(ns) < 2
                else "beyond 2σ — extreme; expect mean-reversion attempts and fast premium swings")
        L.append(f"The open priced a 1-σ day of <b>±{sig:,.0f}</b> (bands {mm['open'] - sig:,.0f} – {mm['open'] + sig:,.0f}; "
                 f"1.5σ {mm['open'] - 1.5 * sig:,.0f} – {mm['open'] + 1.5 * sig:,.0f}). The move is <b>{ns:+.2f} σ</b>: {band}.")
    if np.isfinite(mm["atr_used"]):
        L.append(f"High–low is <b>{mm['atr_used'] * 100:,.0f}%</b> of a normal day (ATR-14 {mm['atr']:,.0f})"
                 + (" — the day's usual range is used up." if mm["atr_used"] >= 1 else "."))
    reach = mm["left"] if np.isfinite(mm["left"]) else (sig * 0.5 if np.isfinite(sig) else 60.0)
    tgt_dn, why_dn, cand_dn = path_levels(mm, spot, "PE", reach)
    tgt_up, why_up, cand_up = path_levels(mm, spot, "CE", reach)
    nxt = lambda cand: " → ".join(f"{n} {v:,.0f}" for n, v in cand[:3]) or "no mapped level"
    L.append(f"Priced move for the rest of today ≈ <b>±{reach:,.0f}</b> pts → realistic band "
             f"<b>{spot - reach:,.0f} – {spot + reach:,.0f}</b>.")
    L.append(f"<b>Further FALL</b>: levels below — {nxt(cand_dn)}. Realistic target today <b>{tgt_dn:,.0f}</b> ({why_dn}).")
    L.append(f"<b>BOUNCE / RISE</b>: levels above — {nxt(cand_up)}. Realistic target today <b>{tgt_up:,.0f}</b> ({why_up}).")
    dtype = a.get("day")
    if dtype:
        L.append(f"Day type: <b>{dtype['kind']}</b> (realised range {dtype['ratio']:.1f}× what the open priced so far).")
    st.markdown(_card("📏 HOW FAR CAN NIFTY GO TODAY?", "<br>".join(L)), unsafe_allow_html=True)

    # ── 2 · which strike ───────────────────────────────────────────────────────────────────────────
    flt = a.get("filter") or {}
    g = {s_: flt.get(s_, {}) for s_ in ("CE", "PE")}
    bias_html = []
    for s_ in ("CE", "PE"):
        x = g[s_]
        if not x:
            continue
        colr = GREEN if "FAVOURED" in x["grade"] else RED if "AVOID" in x["grade"] else GOLD
        bias_html.append(f"<b>{'BUY CE' if s_ == 'CE' else 'BUY PE'}: <span style='color:{colr}'>{x['grade']}</span></b>"
                         f"<br><span style='color:{GREEN}'>+ {' · '.join(x['good'][:4]) or '—'}</span>"
                         f"<br><span style='color:{RED}'>− {' · '.join(x['bad'][:4]) or '—'}</span>")
    if np.isfinite(ns) and abs(ns) >= 1.5:
        with_t = "PE" if mm["move"] < 0 else "CE"
        bias_html.append(f"<span style='color:{GOLD}'>⚠ The move is already {abs(ns):.1f}σ — a new {with_t} needs a "
                         f"pull-back entry (failed bounce) and a smaller target; the counter-trend "
                         f"{'CE' if with_t == 'PE' else 'PE'} needs a confirmed reversal candle (15-min close over the 8 EMA).</span>")
    st.markdown(_card("🧭 WHICH SIDE — writer flow, walls, gamma, IV (same filter as the ★ PRO brief)",
                      "<br><br>".join(bias_html) or "No side read yet."), unsafe_allow_html=True)

    res = {}
    for s_, tl in (("CE", tgt_up), ("PE", tgt_dn)):
        mv = tfix if tmode == "Fixed pts" else max(5.0, abs(tl - spot))
        sc = scenarios(ch, spot, s_, mv, sl, mins, div_win, div_loss)
        best, fast = pick(sc)
        res[s_] = (sc, best, fast, (spot + mv) if s_ == "CE" else (spot - mv))
    cc = st.columns(2)
    for col_, s_ in zip(cc, ("CE", "PE")):
        sc, best, fast, tl = res[s_]
        grade = g[s_].get("grade", "") if g.get(s_) else ""
        col_.markdown(pick_html(s_, best, "★ BEST COURSE PICK", spot, sl, tl, lot, grade), unsafe_allow_html=True)
        col_.markdown(pick_html(s_, fast, "⚡ FAST PICK (ATM / OTM — small size)", spot, sl, tl, lot, grade),
                      unsafe_allow_html=True)

    with st.expander("All strikes — Greeks scenario table"):
        for s_ in ("CE", "PE"):
            sc = res[s_][0]
            if sc is None or sc.empty:
                continue
            st.markdown(f"**{s_}** — target move {abs(res[s_][3] - spot):,.0f} pts in {mins:.0f} min · stop {sl:.0f} pts")
            st.dataframe(pd.DataFrame({
                "Strike": sc.strike, "Type": sc.kind + " " + sc.itm.abs().round(0).astype(int).astype(str),
                "Premium": sc.ltp.round(2), "Δ": sc.delta.round(2), "Γ": sc.gamma.round(4), "θ/day": sc.theta.round(1),
                "θ %": sc.theta_pct_day.round(1), "ν": sc.vega.round(2), "IV": sc.iv.round(1), "IV skew": sc.ivskew.round(1),
                "Win pts": sc.win.round(1), "Stop pts": sc.loss.round(1), "R:R": sc.rr.round(2),
                "Return %": sc.ret_pct.round(0), "Spread %": sc.spread.round(1), "OI": sc.oi,
                "Course ✓": np.where(sc.course, "✓", "")}), hide_index=True, use_container_width=True, height=320)
    with st.expander("How the Strike Advisor works"):
        st.markdown("<ul>" + "".join(f"<li style='margin:3px 0'>{h}</li>" for h in HOW) + "</ul>", unsafe_allow_html=True)
    st.caption(f"Snapshot {pd.Timestamp(a['now']):%H:%M} · expiry {a['expiry']} · lot {lot}. Information only — not "
               "financial advice. First-order Greeks approximation; not backtested.")
