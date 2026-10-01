"""
greeks_scenario.py — "Greeks Scenario Lab" for the 🎯 Strike Selection tab.

From the Greeks lecture (Dr. Priyanka): direction alone is not enough. An option's
premium is moved by FOUR forces at the same time:
    Delta  — how much premium moves per 1 pt of NIFTY (participation)
    Gamma  — how fast that delta itself changes as NIFTY moves (acceleration)
    Theta  — what WAITING costs you, every hour the move does not come
    Vega   — what an IV change adds or removes, even if NIFTY moves your way
    (Rho   — interest-rate sensitivity; tiny for intraday index options, ignored)

This panel answers, for 3 strikes side by side:
  "If NIFTY moves X pts, after H hours, while IV changes by Y — what is my premium?"
and splits the answer into  Delta + Gamma + Theta + Vega  so you see WHY.

It also follows the lecture's 6-step sequence:
  1 View → 2 Timing → 3 Candidate strikes → 4 Greeks check → 5 Select → 6 Define risk
and its warning: Greeks describe EXPOSURE, they are NOT entry signals.

Pricing: Black-Scholes repricing anchored to the live LTP (the model's change is
added to the market premium). Information only — not financial advice.
Settings: strategy1_rules.json → "greeks_scenario".
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go

import config
import greeks as gk

RULES_PATH = Path(__file__).parent / "strategy1_rules.json"
_DEF = {"strike_step": 50, "near_otm_steps": 2, "far_otm_steps": 8,
        "min_delta": 0.30, "max_wait_theta_pct": 15.0, "late_hours": [0, 6, 24, 48, 72]}

NAVY, GOLD, WHITE, MUTED, GREEN, RED, BLUE, PURPLE, ORANGE = (
    "#0b1a33", "#d4af37", "#f1f5f9", "#94a3b8", "#22c55e", "#ef4444", "#38bdf8", "#a78bfa", "#f59e0b")


def _rules() -> dict:
    try:
        r = json.loads(RULES_PATH.read_text()).get("greeks_scenario", {})
    except Exception:
        r = {}
    return {**_DEF, **{k: v for k, v in r.items() if not k.startswith("_")}}


# ═══════════════════════════════════════════════════════════════════════════
# 1 · Maths
# ═══════════════════════════════════════════════════════════════════════════
def _px(s, k, iv, t, call):
    return gk.black_scholes_price(s, k, max(iv, 0.5), max(t, 1e-6), call)


def greeks_at(s, k, iv, t, call) -> dict:
    h = max(s * 0.0005, 1.0)
    p0, pu, pd_ = _px(s, k, iv, t, call), _px(s + h, k, iv, t, call), _px(s - h, k, iv, t, call)
    day = 1 / 365
    return {
        "delta": (pu - pd_) / (2 * h),
        "gamma": (pu - 2 * p0 + pd_) / (h * h),
        "theta": _px(s, k, iv, max(t - day, 1e-6), call) - p0,       # pts per calendar day
        "vega": _px(s, k, iv + 1, t, call) - p0,                       # pts per +1 IV point
    }


def scenario(s, k, iv, t_years, call, ltp, move, hours, d_iv) -> dict:
    """Premium after the scenario, split into forces (sequential attribution, anchored to LTP)."""
    t1 = max(t_years - hours / (24 * 365), 1e-6)
    base = _px(s, k, iv, t_years, call)
    after_time = _px(s, k, iv, t1, call)
    after_iv = _px(s, k, iv + d_iv, t1, call)
    final = _px(s + move, k, iv + d_iv, t1, call)
    g1 = greeks_at(s, k, iv + d_iv, t1, call)
    theta_eff = after_time - base
    vega_eff = after_iv - after_time
    path = final - after_iv
    delta_eff = g1["delta"] * move
    gamma_eff = path - delta_eff
    start = ltp if ltp and ltp > 0 else base
    end = max(start + theta_eff + vega_eff + path, 0.05)
    return dict(start=start, end=end, delta_eff=delta_eff, gamma_eff=gamma_eff,
                theta_eff=theta_eff, vega_eff=vega_eff, change=end - start,
                change_pct=(end - start) / start * 100 if start else np.nan)


def breakeven_move(s, k, iv, t, call, ltp, hours, d_iv) -> float | None:
    """NIFTY pts needed (in your direction) for premium to just get back to entry."""
    sign = 1 if call else -1
    f = lambda m: scenario(s, k, iv, t, call, ltp, sign * m, hours, d_iv)["change"]
    if f(0) >= 0:
        return 0.0
    lo, hi = 0.0, 1500.0
    if f(hi) < 0:
        return None
    for _ in range(40):
        mid = (lo + hi) / 2
        if f(mid) < 0:
            lo = mid
        else:
            hi = mid
    return hi


def pick_strikes(df: pd.DataFrame, spot: float, call: bool, r: dict) -> list[float]:
    step = float(r["strike_step"])
    atm = round(spot / step) * step
    sgn = 1 if call else -1
    want = [atm, atm + sgn * r["near_otm_steps"] * step, atm + sgn * r["far_otm_steps"] * step]
    have = set(pd.to_numeric(df["strike"], errors="coerce").dropna())
    return [min(have, key=lambda x: abs(x - w)) for w in want] if have else want


def _row(df, k):
    m = df[pd.to_numeric(df["strike"], errors="coerce") == k]
    return m.iloc[0] if len(m) else None


# ═══════════════════════════════════════════════════════════════════════════
# 2 · Render
# ═══════════════════════════════════════════════════════════════════════════
def _box(title, body_html):
    import streamlit as st
    st.markdown(f"""<div style="background:{NAVY};border-left:4px solid {GOLD};border-radius:6px;padding:12px 16px;
        margin:8px 0 18px 0;color:{WHITE};font-size:0.92rem;line-height:1.55">
        <div style="color:{GOLD};font-weight:700;margin-bottom:4px">{title}</div>{body_html}</div>""", unsafe_allow_html=True)


def render(df: pd.DataFrame, spot: float, direction: str, dte_days: float,
           target_pts: float, stop_pts: float, lot_size: int) -> None:
    import streamlit as st
    r = _rules()
    call = direction.upper().startswith("C")
    side = "ce" if call else "pe"
    st.markdown("### 🧮 Greeks Scenario Lab — what will my premium ACTUALLY do?")
    st.caption("Direction can be right and the option can still disappoint. Delta, Gamma, Theta and Vega act at the SAME time — "
               "this shows each one's share for three strikes side by side.")

    strikes_all = sorted(pd.to_numeric(df["strike"], errors="coerce").dropna().unique())
    default = pick_strikes(df, spot, call, r)
    c1, c2, c3, c4 = st.columns([2.2, 1.2, 1.2, 1.2])
    picks = c1.multiselect("Strikes to compare (ATM · near OTM · far OTM)", strikes_all, default=default,
                           format_func=lambda k: f"{k:,.0f} {direction}", key="gs_strikes", max_selections=4)
    move_dir = 1 if call else -1
    move = c2.number_input("NIFTY move in your favour (pts)", 0, 1000, int(target_pts or 100), 10, key="gs_move")
    hours = c3.number_input("Move comes after (hours)", 0.0, max(dte_days * 24, 1.0), 0.0, 1.0, key="gs_hours",
                            help="0 = immediately. Market hours AND overnight both count (calendar time).")
    d_iv = c4.number_input("IV change (points)", -10.0, 10.0, 0.0, 0.5, key="gs_div",
                           help="e.g. −3 = IV falls from 12% to 9% (after an event)")
    if not picks:
        st.info("Pick at least one strike.")
        return
    t = max(dte_days, 0.01) / 365

    rows, scen = [], {}
    for k in picks:
        row = _row(df, k)
        if row is None:
            continue
        iv = pd.to_numeric(row.get(f"{side}_iv"), errors="coerce")
        if pd.isna(iv) or iv <= 0:
            iv = pd.to_numeric(row.get("iv_used"), errors="coerce")
        if pd.isna(iv) or iv <= 0:
            continue
        ltp = float(pd.to_numeric(row.get(f"{side}_ltp"), errors="coerce") or 0)
        g = greeks_at(spot, k, iv, t, call)
        sc = scenario(spot, k, iv, t, call, ltp, move_dir * move, hours, d_iv)
        be = breakeven_move(spot, k, iv, t, call, ltp, hours, d_iv)
        scen[k] = sc
        rows.append(dict(k=k, iv=iv, ltp=sc["start"], **g, **sc, be=be))
    if not rows:
        st.warning("No usable IV for these strikes.")
        return

    # Table
    tbl = pd.DataFrame([{
        "Strike": f"{x['k']:,.0f} {direction}", "Premium": f"₹{x['ltp']:.1f}", "IV": f"{x['iv']:.1f}%",
        "Δ Delta": f"{abs(x['delta']):.2f}", "Γ Gamma": f"{x['gamma']:.4f}", "Θ /day": f"{x['theta']:+.1f}",
        "Vega /1 IV": f"{x['vega']:+.1f}",
        "→ Delta effect": f"{x['delta_eff']:+.1f}", "→ Gamma bonus": f"{x['gamma_eff']:+.1f}",
        "→ Time decay": f"{x['theta_eff']:+.1f}", "→ IV effect": f"{x['vega_eff']:+.1f}",
        "Premium after": f"₹{x['end']:.1f}", "Change": f"{x['change']:+.1f} ({x['change_pct']:+.0f}%)",
        "₹ / lot": f"{x['change'] * lot_size:+,.0f}",
        "Break-even move": ("—" if x["be"] is None else f"{x['be']:.0f} pts"),
    } for x in rows])
    st.dataframe(tbl, hide_index=True, use_container_width=True)

    # Waterfall-style grouped bars
    fig = go.Figure()
    names = [f"{x['k']:,.0f}" for x in rows]
    for key, label, col in (("delta_eff", "Delta (move)", BLUE), ("gamma_eff", "Gamma (acceleration)", PURPLE),
                            ("theta_eff", "Theta (waiting)", ORANGE), ("vega_eff", "Vega (IV change)", "#e879f9")):
        fig.add_trace(go.Bar(x=names, y=[x[key] for x in rows], name=label, marker_color=col,
                             text=[f"{x[key]:+.1f}" for x in rows], textposition="outside"))
    fig.add_trace(go.Scatter(x=names, y=[x["change"] for x in rows], name="NET change", mode="markers+text",
                             marker=dict(size=16, color=[GREEN if x["change"] >= 0 else RED for x in rows], symbol="diamond"),
                             text=[f"NET {x['change']:+.1f}" for x in rows], textposition="top center",
                             textfont=dict(color=WHITE)))
    fig.update_layout(barmode="group", title=dict(text="<b>Where the premium change comes from (option pts)</b>",
                      font=dict(color=GOLD)), paper_bgcolor=NAVY, plot_bgcolor="#10223f", font=dict(color=WHITE),
                      height=420, margin=dict(l=10, r=10, t=60, b=10),
                      legend=dict(orientation="h", y=1.12, x=1, xanchor="right", font=dict(color=WHITE)))
    fig.update_xaxes(type="category")
    fig.update_yaxes(gridcolor="#1e3358", zerolinecolor=MUTED)
    st.plotly_chart(fig, use_container_width=True, key="gs_fig")

    # Late-move grid
    late = []
    for x in rows:
        rec = {"Strike": f"{x['k']:,.0f} {direction}"}
        for h in r["late_hours"]:
            if h > dte_days * 24:
                continue
            s2 = scenario(spot, x["k"], x["iv"], t, call, x["ltp"], move_dir * move, h, d_iv)
            rec[("now" if h == 0 else f"after {h}h")] = f"{s2['change']:+.1f} ({s2['change_pct']:+.0f}%)"
        late.append(rec)
    st.markdown(f"**Same {move}-pt move, but LATE** — premium change if the move takes this long (IV change {d_iv:+.1f}):")
    st.dataframe(pd.DataFrame(late), hide_index=True, use_container_width=True)

    # Plain-English reading
    best = max(rows, key=lambda x: x["change_pct"])
    worst = min(rows, key=lambda x: x["change_pct"])
    lines = [f"If NIFTY moves <b>{move} pts</b> your way after <b>{hours:.0f}h</b> with IV <b>{d_iv:+.1f}</b>: "
             f"<b>{best['k']:,.0f}</b> does best ({best['change']:+.1f} pts, {best['change_pct']:+.0f}%); "
             f"<b>{worst['k']:,.0f}</b> does worst ({worst['change']:+.1f} pts, {worst['change_pct']:+.0f}%)."]
    for x in rows:
        notes = []
        if abs(x["delta"]) < r["min_delta"]:
            notes.append(f"delta only {abs(x['delta']):.2f} → captures little of the move")
        if x["theta_eff"] < 0 and abs(x["theta_eff"]) / x["ltp"] * 100 > r["max_wait_theta_pct"]:
            notes.append(f"waiting {hours:.0f}h costs {abs(x['theta_eff']) / x['ltp'] * 100:.0f}% of premium")
        if d_iv < 0 and x["vega_eff"] < -0.25 * max(x["delta_eff"], 1e-9):
            notes.append(f"IV fall eats {abs(x['vega_eff']):.1f} pts of the gain")
        if x["gamma_eff"] > 0.25 * max(x["delta_eff"], 1e-9):
            notes.append(f"gamma adds {x['gamma_eff']:.1f} pts as delta grows")
        if x["be"] is not None and x["be"] > 0:
            notes.append(f"needs {x['be']:.0f} pts just to break even")
        if notes:
            lines.append(f"<b>{x['k']:,.0f}</b>: " + "; ".join(notes) + ".")
    _box("RIGHT NOW · READING", "<br>".join(lines))

    # 6-step checklist from the lecture
    in_band = [x for x in rows if abs(x["delta"]) >= r["min_delta"]]
    in_band_txt = ", ".join(f"{x['k']:,.0f}" for x in in_band) or "none"
    steps = [
        ("1 · View", True, f"Direction chosen: <b>{'Bullish (CE)' if call else 'Bearish (PE)'}</b> — must come from price action / structure / context, not from Greeks."),
        ("2 · Timing", hours >= 0, f"You expect the move within <b>{hours:.0f}h</b>; {dte_days:.1f} days to expiry."),
        ("3 · Candidate strikes", len(rows) >= 2, f"{len(rows)} strikes compared."),
        ("4 · Greeks check", bool(in_band), f"Delta ≥ {r['min_delta']}: <b>{in_band_txt}</b>. "
                                            "Check gamma (acceleration), theta (cost of waiting), vega (IV risk) in the table."),
        ("5 · Select", bool(in_band), f"Best fit for this scenario: <b>{max(in_band or rows, key=lambda x: x['change_pct'])['k']:,.0f} {direction}</b>."),
        ("6 · Define risk", stop_pts > 0, f"Stop set at <b>{stop_pts:.0f} spot pts</b> (≈ ₹{abs(rows[0]['delta']) * stop_pts * lot_size:,.0f}/lot on the first strike)."),
    ]
    body = "".join(f"<div style='margin:3px 0'><span style='color:{GREEN if ok else RED}'>{'✔' if ok else '✘'}</span> "
                   f"<b style='color:{GOLD}'>{name}</b> — {txt}</div>" for name, ok, txt in steps)
    body += (f"<div style='margin-top:8px;color:#fca5a5'>⚠ Greeks are NOT entry signals. High delta ≠ buy, high gamma ≠ 'will explode', "
             f"high theta ≠ become a seller, high vega ≠ buy. They describe how the contract will EXPERIENCE your move.</div>")
    _box("THE 6-STEP SEQUENCE (view → timing → strike → Greeks → select → risk)", body)

    _box("HOW TO READ", "<ul style='margin:2px 0 0 18px;color:#cbd5e1'>"
         "<li><b>Delta</b> = option pts per 1 NIFTY pt. ATM ≈ 0.50, far OTM ≈ 0.10–0.15 → same move, much smaller gain.</li>"
         "<li><b>Gamma</b> = how fast delta grows as NIFTY moves your way. Biggest near ATM and near expiry — why premium "
         "suddenly 'accelerates' on expiry days. For sellers the same thing makes losses grow fast.</li>"
         "<li><b>Theta</b> = cost of WAITING. 'Market didn't go against me' ≠ safe — premium still bleeds. "
         "Being right eventually ≠ being right in time.</li>"
         "<li><b>Vega</b> = effect of IV change. NIFTY can go your way and IV crush (e.g. 12% → 9% after an event) can still eat the gain. "
         "IV does not tell direction.</li>"
         "<li><b>Break-even move</b> = how many points NIFTY must move just for you not to lose, given the waiting time and IV change.</li>"
         "<li>Rho (interest rates) is ignored — negligible for intraday index options.</li></ul>")
