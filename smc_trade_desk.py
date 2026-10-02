"""
smc_trade_desk.py — one clear SMC verdict for the 🏹 Breakout / Trap tab.

What a discretionary SMC trader asks, in order:
  1. TREND      15m structure: up / down / sideways. Did it just CHANGE (CHoCH)?
  2. PHASE      trending, CONSOLIDATING (tight range, no BOS), or REVERSING (fresh CHoCH)
  3. LOCATION   where is price in the dealing range: cheap (discount) / expensive (premium)?
                is it sitting in DEMAND (bullish OB/FVG) or SUPPLY (bearish OB/FVG)?
  4. LIQUIDITY  which highs/lows are still UNTAKEN (targets) and which are already SWEPT
                (no longer targets — a swept pool is spent fuel)
  5. ACTION     BUY / SELL / WAIT / AVOID + entry zone, SL zone, TP1/TP2, R:R, invalidation

Both directions are evaluated. Closed candles only (smc.py pivots are confirmed with lag).
Also fixes VWAP: session_vwap() resets every day and is hidden when there is no real volume.

Settings: strategy1_rules.json → "smc_trade_desk". Information only — not financial advice.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import time as dtime
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go

import smc

RULES_PATH = Path(__file__).parent / "strategy1_rules.json"
_DEF = {"pivot_n": 2, "consolidation_bars": 12, "consolidation_atr_mult": 3.0, "min_rr": 1.5,
        "sl_buffer_pts": 5.0, "no_new_trades_after": "15:00", "zone_proximity_pts": 10.0,
        "choch_recent_bars": 8, "premium_avoid_pct": 0.8, "discount_avoid_pct": 0.2}
NAVY, PANEL, GRID, GOLD, WHITE, MUTED = "#0b1a33", "#0f1724", "#1e293b", "#d4af37", "#f1f5f9", "#94a3b8"
GREEN, RED, BLUE, AMBER, PURPLE = "#22c55e", "#ef4444", "#38bdf8", "#f59e0b", "#a78bfa"


def _rules() -> dict:
    try:
        r = json.loads(RULES_PATH.read_text()).get("smc_trade_desk", {})
    except Exception:
        r = {}
    return {**_DEF, **{k: v for k, v in r.items() if not k.startswith("_")}}


# ═══════════════════════════════════════════════════════════════════════════
# VWAP fix
# ═══════════════════════════════════════════════════════════════════════════
def session_vwap(df: pd.DataFrame) -> pd.Series | None:
    """VWAP that RESETS each session. None when volume is missing/zero (index candles)."""
    if df is None or "volume" not in df or "timestamp" not in df:
        return None
    v = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
    if v.sum() <= 0:
        return None
    tp = (df["high"] + df["low"] + df["close"]) / 3
    day = pd.to_datetime(df["timestamp"]).dt.date
    pv = (tp * v).groupby(day).cumsum()
    vv = v.groupby(day).cumsum().replace(0, np.nan)
    return pv / vv


# ═══════════════════════════════════════════════════════════════════════════
# Compute
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class Desk:
    action: str = "WAIT"             # BUY | SELL | WAIT | AVOID
    headline: str = ""
    reasons: list = field(default_factory=list)
    trend15: str = "TRANSITION"
    trend_tf: str = "TRANSITION"
    phase: str = "TRENDING"          # TRENDING | CONSOLIDATION | REVERSAL
    trend_change: str = ""           # banner text if a CHoCH just printed
    entry: tuple | None = None       # (lo, hi)
    sl: float | None = None
    tp1: float | None = None
    tp2: float | None = None
    tp1_name: str = ""
    tp2_name: str = ""
    rr: float | None = None
    invalidation: str = ""
    range_box: tuple | None = None   # (lo, hi, i0)
    position_pct: float | None = None
    demand: list = field(default_factory=list)   # (lo, hi, i, label)
    supply: list = field(default_factory=list)
    liq_above: list = field(default_factory=list)  # (price, name, swept, i)
    liq_below: list = field(default_factory=list)
    events: list = field(default_factory=list)     # (i, at, kind, direction, level)
    spot: float = 0.0


def _zones(r: dict, spot: float):
    dem, sup = [], []
    for ob in r.get("open_obs", []):
        (dem if ob.direction == "BULLISH" else sup).append((ob.low, ob.high, ob.i, "Order block"))
    for b in r.get("active_breakers", []):
        d = getattr(b, "breaker_direction", "")
        (dem if d == "BULLISH" else sup).append((b.low, b.high, b.i, "Breaker"))
    for g in r.get("open_fvgs", []):
        (dem if g.direction == "BULLISH" else sup).append((g.bottom, g.top, g.i, "FVG"))
    dem = sorted([z for z in dem if z[0] < spot + 5], key=lambda z: -z[1])
    sup = sorted([z for z in sup if z[1] > spot - 5], key=lambda z: z[0])
    return dem, sup


def _liquidity(r: dict, df: pd.DataFrame, spot: float):
    above, below = [], []
    last = len(df) - 1
    for p in r.get("pivots", []):
        if not p.known_at(last):
            continue
        swept = p.violated_i is not None
        if p.kind == "HIGH" and (p.price > spot or swept):
            above.append((p.price, f"Swing high {p.at:%H:%M}", swept, p.i))
        if p.kind == "LOW" and (p.price < spot or swept):
            below.append((p.price, f"Swing low {p.at:%H:%M}", swept, p.i))
    for pool in r.get("liquidity_pools", []):
        lst = above if pool.kind == "EQH" else below
        lst.append((pool.level, "Equal highs" if pool.kind == "EQH" else "Equal lows", pool.swept, pool.first_i))
    for lv in r.get("liquidity_levels", []):
        if lv.name in ("PDH", "PDL", "PWH", "PWL"):
            lst = above if lv.kind == "BUY_SIDE" else below
            lst.append((lv.level, lv.name, lv.swept, -1))
    above = sorted(above, key=lambda x: x[0])
    below = sorted(below, key=lambda x: -x[0])
    return above, below


def _consolidation(df: pd.DataFrame, events: list, rr: dict):
    k = int(rr["consolidation_bars"])
    if len(df) < k + 20:
        return None
    rng = (df["high"] - df["low"]).tail(60).mean()
    w = df.tail(k)
    hi, lo = float(w["high"].max()), float(w["low"].min())
    recent_bos = [e for e in events if e.i >= len(df) - k]
    if hi - lo <= rr["consolidation_atr_mult"] * rng and not recent_bos:
        return (lo, hi, len(df) - k)
    return None


def compute(df: pd.DataFrame, df15: pd.DataFrame | None, rr: dict | None = None) -> Desk:
    rr = rr or _rules()
    n = int(rr["pivot_n"])
    d = Desk()
    r = smc.read(df, n=n)
    r15 = smc.read(df15, n=n) if df15 is not None and len(df15) >= 15 else r
    spot = float(df["close"].iloc[-1])
    d.spot = spot
    st = r["structure"]
    d.trend_tf = st.trend
    d.trend15 = r15["structure"].trend
    d.events = [(e.i, e.at, e.kind, e.direction, e.level) for e in st.events]
    d.demand, d.supply = _zones(r, spot)
    d.liq_above, d.liq_below = _liquidity(r, df, spot)
    dr = r.get("dealing_range") or r.get("macro_dealing_range")
    d.position_pct = float(dr.position_pct) if dr else None
    last_i = len(df) - 1

    # Trend change banner
    mss = [e for e in st.events if e.is_mss or e.kind in ("CHOCH", "MSS")]
    if mss and last_i - mss[-1].i <= rr["choch_recent_bars"]:
        e = mss[-1]
        frm = "DOWN → UP" if e.direction == "BULLISH" else "UP → DOWN"
        d.trend_change = (f"TREND CHANGE {frm}: candle closed {'above' if e.direction == 'BULLISH' else 'below'} "
                          f"{e.level:,.0f} at {e.at:%H:%M} (CHoCH). First pullback after this is the classic entry.")
        d.phase = "REVERSAL"

    box = _consolidation(df, st.events, rr)
    if box:
        d.phase, d.range_box = "CONSOLIDATION", box

    # Bias: latest structure event on trigger TF, filtered by 15m
    last_ev = st.events[-1] if st.events else None
    bias = last_ev.direction if last_ev else None
    if d.trend15 in ("BULLISH", "BEARISH") and bias and bias != d.trend15 and d.phase != "REVERSAL":
        bias = None
    ts = pd.to_datetime(df["timestamp"].iloc[-1]) if "timestamp" in df else None
    late = ts is not None and ts.time() >= dtime(*map(int, rr["no_new_trades_after"].split(":")))

    # Range position on the CURRENT LEG (since the last opposite-direction structure event)
    if bias in ("BULLISH", "BEARISH") and st.events:
        opp = [e for e in st.events if e.direction != bias]
        start = max(0, (opp[-1].i if opp else 0) - 3)
        leg = df.iloc[start:]
        if bias == "BULLISH":
            j = int(leg["low"].values.argmin())
            lo_l, hi_l = float(leg["low"].iloc[j]), float(leg["high"].iloc[j:].max())
        else:
            j = int(leg["high"].values.argmax())
            hi_l, lo_l = float(leg["high"].iloc[j]), float(leg["low"].iloc[j:].min())
        if hi_l > lo_l:
            d.position_pct = (spot - lo_l) / (hi_l - lo_l)

    unswept_up = [x for x in d.liq_above if not x[2] and x[0] > spot]
    unswept_dn = [x for x in d.liq_below if not x[2] and x[0] < spot]
    swept_up = [x for x in d.liq_above if x[2]]
    swept_dn = [x for x in d.liq_below if x[2]]
    near = rr["zone_proximity_pts"]
    pos = d.position_pct
    in_dem = next((z for z in d.demand if z[0] - near <= spot <= z[1] + near), None)
    in_sup = next((z for z in d.supply if z[0] - near <= spot <= z[1] + near), None)
    pos = d.position_pct

    def plan(side):
        buf = rr["sl_buffer_pts"]
        if side == "BUY":
            z = in_dem or (d.demand[0] if d.demand else None)
            entry = (z[0], z[1]) if z else (spot - 10, spot)
            if entry[1] - entry[0] < 6:
                entry = (entry[0] - 3, entry[1] + 3)
            prot = [x[0] for x in d.liq_below if x[0] < entry[0]]
            sl = (min(entry[0], max(prot) if prot else entry[0]) - buf)
            tps = [x for x in unswept_up if x[0] > entry[1]] or [(z2[0], "Supply zone", False, 0) for z2 in d.supply if z2[0] > entry[1]]
            mid = (entry[0] + entry[1]) / 2
        else:
            z = in_sup or (d.supply[0] if d.supply else None)
            entry = (z[0], z[1]) if z else (spot, spot + 10)
            if entry[1] - entry[0] < 6:
                entry = (entry[0] - 3, entry[1] + 3)
            prot = [x[0] for x in d.liq_above if x[0] > entry[1]]
            sl = (max(entry[1], min(prot) if prot else entry[1]) + buf)
            tps = [x for x in unswept_dn if x[0] < entry[0]] or [(z2[1], "Demand zone", False, 0) for z2 in d.demand if z2[1] < entry[0]]
            mid = (entry[0] + entry[1]) / 2
        d.entry, d.sl = entry, sl
        if tps:
            d.tp1, d.tp1_name = tps[0][0], tps[0][1]
            if len(tps) > 1:
                d.tp2, d.tp2_name = tps[1][0], tps[1][1]
            risk = abs(mid - sl)
            d.rr = abs(d.tp1 - mid) / risk if risk > 0 else None
        d.invalidation = (f"15m/5m close below {sl:,.0f} = idea wrong" if side == "BUY"
                          else f"15m/5m close above {sl:,.0f} = idea wrong")

    R = d.reasons
    recent = len(df) - 40
    sw_lo = [x for x in swept_dn if x[0] < spot and x[3] >= recent]
    sw_hi = [x for x in swept_up if x[0] > spot and x[3] >= recent]
    sw_lo_b = [x for x in swept_dn if x[0] < spot and x[3] >= recent]
    if bias == "BULLISH" and sw_lo:
        R.append(f"Sell-side liquidity swept at {min(x[0] for x in sw_lo):,.0f} then structure shifted UP — "
                 f"classic stop-hunt → reversal. That low is now the PROTECTED low (SL goes below it) and is no longer a target.")
    if bias == "BEARISH" and sw_hi:
        R.append(f"Buy-side liquidity swept at {max(x[0] for x in sw_hi):,.0f} then structure shifted DOWN — "
                 f"classic stop-hunt → reversal. That high is now the PROTECTED high (SL goes above it).")
    if bias == "BULLISH" and sw_hi:
        R.append(f"High at {min(x[0] for x in sw_hi):,.0f} already taken — spent, skipped as a target.")
    if bias == "BEARISH" and sw_lo:
        R.append(f"Low at {max(x[0] for x in sw_lo):,.0f} already taken — spent, skipped as a target.")

    if late:
        d.action, d.headline = "AVOID", f"No new trades after {rr['no_new_trades_after']} — manage open positions only."
    elif d.phase == "CONSOLIDATION":
        lo, hi, _ = d.range_box
        d.action = "WAIT"
        d.headline = f"CONSOLIDATION {lo:,.0f}–{hi:,.0f}: no edge inside the box."
        R.append(f"Breakout trigger: candle CLOSE above {hi:,.0f} (bullish) or below {lo:,.0f} (bearish). "
                 f"A wick outside that closes back inside = trap → trade the other way.")
    elif bias == "BULLISH":
        if in_sup or (pos is not None and pos >= rr["premium_avoid_pct"]):
            d.action = "AVOID"
            where = f"supply {in_sup[0]:,.0f}–{in_sup[1]:,.0f}" if in_sup else f"premium ({pos * 100:.0f}% of range)"
            d.headline = f"Trend is UP but price is at {where} — don't chase longs here."
            if d.demand:
                R.append(f"Wait for a pullback into demand {d.demand[0][0]:,.0f}–{d.demand[0][1]:,.0f} to buy.")
            plan("BUY")
        elif in_dem or (pos is not None and pos <= 0.5):
            plan("BUY")
            ok = d.rr is not None and d.rr >= rr["min_rr"]
            d.action = "BUY" if ok else "WAIT"
            inside = d.entry[0] <= spot <= d.entry[1] + near
            d.headline = (("Uptrend + price INSIDE demand → BUY now in the zone." if inside else
                           f"Uptrend + discount → BUY LIMIT on pullback into {d.entry[0]:,.0f}–{d.entry[1]:,.0f}.") if ok
                          else f"Uptrend + discount, but R:R {d.rr or 0:.1f} < {rr['min_rr']} — wait for better price.")
        else:
            plan("BUY")
            d.action = "WAIT"
            d.headline = "Uptrend, price mid-range — wait for the pullback into the entry zone."
    elif bias == "BEARISH":
        if in_dem or (pos is not None and pos <= rr["discount_avoid_pct"]):
            d.action = "AVOID"
            where = f"demand {in_dem[0]:,.0f}–{in_dem[1]:,.0f}" if in_dem else f"discount ({pos * 100:.0f}% of range)"
            d.headline = f"Trend is DOWN but price is at {where} — don't chase shorts here."
            if d.supply:
                R.append(f"Wait for a rally into supply {d.supply[0][0]:,.0f}–{d.supply[0][1]:,.0f} to sell.")
            plan("SELL")
        elif in_sup or (pos is not None and pos >= 0.5):
            plan("SELL")
            ok = d.rr is not None and d.rr >= rr["min_rr"]
            d.action = "SELL" if ok else "WAIT"
            inside = d.entry[0] - near <= spot <= d.entry[1]
            d.headline = (("Downtrend + price INSIDE supply → SELL now in the zone." if inside else
                           f"Downtrend + premium → SELL LIMIT on rally into {d.entry[0]:,.0f}–{d.entry[1]:,.0f}.") if ok
                          else f"Downtrend + premium, but R:R {d.rr or 0:.1f} < {rr['min_rr']} — wait for better price.")
        else:
            plan("SELL")
            d.action = "WAIT"
            d.headline = "Downtrend, price mid-range — wait for the rally into the entry zone."
    else:
        d.action = "WAIT"
        d.headline = (f"{'5m'} and 15m disagree (trigger {d.trend_tf.lower()}, 15m {d.trend15.lower()}) — no trade until they align, "
                      f"or until the {d.trend15.lower()} 15m trend flips with its own CHoCH."
                      if d.trend_tf != d.trend15 else "No clear structure yet.")
    if d.tp1 is None and d.action in ("BUY", "SELL"):
        d.action, d.headline = "WAIT", d.headline + " (no untaken liquidity left as a target)"
    return d


# ═══════════════════════════════════════════════════════════════════════════
# Chart
# ═══════════════════════════════════════════════════════════════════════════
def chart(df: pd.DataFrame, d: Desk, title: str):
    x = pd.to_datetime(df["timestamp"]) if "timestamp" in df else df.index
    xe = x.iloc[-1] + (x.iloc[-1] - x.iloc[-2]) * 6
    fig = go.Figure(go.Candlestick(x=x, open=df["open"], high=df["high"], low=df["low"], close=df["close"],
                                   name="NIFTY", increasing_line_color=GREEN, decreasing_line_color=RED,
                                   increasing_fillcolor=GREEN, decreasing_fillcolor=RED))
    vw = session_vwap(df)
    if vw is not None:
        fig.add_trace(go.Scatter(x=x, y=vw, name="Session VWAP", line=dict(color=BLUE, width=1.4)))
    def _dedupe(zs):
        out = []
        for z in zs:
            if all(z[1] < o[0] - 2 or z[0] > o[1] + 2 for o in out):
                out.append(z)
        return out[:2]
    for lo, hi, i, lab in _dedupe(d.demand):
        fig.add_shape(type="rect", x0=x.iloc[max(i, 0)], x1=xe, y0=lo, y1=hi, fillcolor="rgba(34,197,94,0.14)",
                      line=dict(color=GREEN, width=1))
        fig.add_annotation(x=xe, y=hi, text=f"DEMAND ({lab})", showarrow=False, xanchor="right", yanchor="bottom",
                           font=dict(color=GREEN, size=10))
    for lo, hi, i, lab in _dedupe(d.supply):
        fig.add_shape(type="rect", x0=x.iloc[max(i, 0)], x1=xe, y0=lo, y1=hi, fillcolor="rgba(239,68,68,0.14)",
                      line=dict(color=RED, width=1))
        fig.add_annotation(x=xe, y=lo, text=f"SUPPLY ({lab})", showarrow=False, xanchor="right", yanchor="top",
                           font=dict(color=RED, size=10))
    tday = pd.to_datetime(df["timestamp"]).dt.date if "timestamp" in df else None
    i0 = int(np.argmax((tday == tday.iloc[-1]).to_numpy())) if tday is not None else 0
    lo_v, hi_v = float(df["low"].iloc[i0:].min()) - 40, float(df["high"].iloc[i0:].max()) + 40
    for lst, sgn in ((d.liq_above, 1), (d.liq_below, -1)):
        keep = [x for x in lst if lo_v <= x[0] <= hi_v and (x[3] >= i0 or x[3] == -1)]
        un = sorted([x for x in keep if not x[2]], key=lambda x: abs(x[0] - d.spot))[:2]
        sw = sorted([x for x in keep if x[2]], key=lambda x: abs(x[0] - d.spot))[:2]
        shown = []
        for price, name, swept, i in un + sw:
            if any(abs(price - s) < 4 for s in shown):
                continue
            shown.append(price)
            fig.add_hline(y=price, line=dict(color=MUTED if swept else AMBER, width=1, dash="dot" if swept else "dash"))
            fig.add_annotation(x=x.iloc[i0], y=price, xanchor="left", yanchor="bottom", showarrow=False,
                               text=(f"✕ {name} {price:,.0f} — TAKEN" if swept else f"{name} {price:,.0f} — untaken liquidity"),
                               font=dict(color=MUTED if swept else AMBER, size=10))
    for i, at, kind, direction, lvl in d.events[-8:]:
        if 0 <= i < len(x):
            lab = "CHoCH" if kind in ("CHOCH", "MSS") else "BOS"
            fig.add_annotation(x=x.iloc[i], y=lvl, text=f"{lab} {'▲' if direction == 'BULLISH' else '▼'}",
                               showarrow=True, arrowhead=2, ax=0, ay=-26 if direction == "BULLISH" else 26,
                               font=dict(color=GREEN if direction == "BULLISH" else RED, size=11),
                               arrowcolor=GREEN if direction == "BULLISH" else RED)
    if d.range_box:
        lo, hi, i0 = d.range_box
        fig.add_shape(type="rect", x0=x.iloc[i0], x1=xe, y0=lo, y1=hi, fillcolor="rgba(245,158,11,0.10)",
                      line=dict(color=AMBER, width=1.5, dash="dot"))
    if d.entry:
        fig.add_shape(type="rect", x0=x.iloc[-1], x1=xe, y0=d.entry[0], y1=d.entry[1], fillcolor="rgba(56,189,248,0.25)",
                      line=dict(color=BLUE, width=1.5))
        fig.add_annotation(x=xe, y=d.entry[1], text="ENTRY ZONE", showarrow=False, xanchor="right", yanchor="bottom",
                           font=dict(color=BLUE, size=11))
    if d.sl:
        fig.add_shape(type="line", x0=x.iloc[-1], x1=xe, y0=d.sl, y1=d.sl, line=dict(color=RED, width=2))
        fig.add_annotation(x=xe, y=d.sl, text=f"SL {d.sl:,.0f}", showarrow=False, xanchor="right",
                           yanchor="top" if d.action != "SELL" else "bottom", font=dict(color=RED, size=11))
    for tp, name, lab in ((d.tp1, d.tp1_name, "TP1"), (d.tp2, d.tp2_name, "TP2")):
        if tp:
            fig.add_shape(type="line", x0=x.iloc[-1], x1=xe, y0=tp, y1=tp, line=dict(color=GREEN, width=2))
            fig.add_annotation(x=xe, y=tp, text=f"{lab} {tp:,.0f}", showarrow=False, xanchor="right", yanchor="bottom",
                               font=dict(color=GREEN, size=11))
    fig.update_layout(title=dict(text=f"<b>{title}</b>", font=dict(color=GOLD, size=15)), paper_bgcolor=NAVY,
                      plot_bgcolor=PANEL, font=dict(color=WHITE), height=620, margin=dict(l=10, r=10, t=50, b=10),
                      xaxis_rangeslider_visible=False, showlegend=False, hovermode="x unified")
    fig.update_xaxes(gridcolor=GRID, range=[x.iloc[i0], xe],
                     rangebreaks=[dict(bounds=[15.6, 9.2], pattern="hour"), dict(bounds=["sat", "mon"])])
    fig.update_yaxes(gridcolor=GRID, range=[lo_v, hi_v])
    return fig


# ═══════════════════════════════════════════════════════════════════════════
# Render
# ═══════════════════════════════════════════════════════════════════════════
def _today(df: pd.DataFrame) -> pd.DataFrame:
    if "timestamp" not in df:
        return df
    t = pd.to_datetime(df["timestamp"])
    last_day = t.dt.date.iloc[-1]
    days = sorted(t.dt.date.unique())
    keep = days[-2:] if len(days) >= 2 else days            # today + yesterday for context
    return df[t.dt.date.isin(keep)].reset_index(drop=True)


def render_five_rule(df_full: pd.DataFrame, df15: pd.DataFrame | None, frames: dict | None) -> None:
    """5-Rule SMC model checklist (smc_five_rule.py)."""
    import streamlit as st
    import smc_five_rule as fr
    try:
        d = df_full.copy()
        if "closed" in d:
            d = d[d["closed"].fillna(True).astype(bool)]
        d = d.reset_index(drop=True)
        d1 = (frames or {}).get("1m")
        if d1 is not None:
            d1 = d1.copy()
            if "timestamp" not in d1 and isinstance(d1.index, pd.DatetimeIndex):
                d1 = d1.reset_index().rename(columns={d1.index.name or "index": "timestamp"})
            if "closed" in d1:
                d1 = d1[d1["closed"].fillna(True).astype(bool)]
            d1 = d1.reset_index(drop=True)
        now = pd.to_datetime(d["timestamp"]).iloc[-1].to_pydatetime()
        res = fr.evaluate(d1, d, df15, now, fr.best_daily(d1 if d1 is not None and len(d1) > len(d) * 3 else d))
    except Exception as e:                                   # noqa: BLE001
        st.caption(f"5-Rule model unavailable: {e}")
        return
    col = GREEN if "TAKE" in res.verdict and "BUY" in res.verdict else RED if "TAKE" in res.verdict else \
        AMBER if "READY" in res.verdict or "VALID" in res.verdict else "#64748b"
    rows = "".join(
        f"<div style='display:flex;gap:10px;padding:6px 0;border-bottom:1px solid {GRID}'>"
        f"<span style='color:{GREEN if ok else RED};font-weight:800;width:18px'>{'✔' if ok else '✘'}</span>"
        f"<span style='color:{GOLD};font-weight:700;min-width:170px'>{nm}</span>"
        f"<span style='color:#cbd5e1'>{det}</span></div>" for nm, ok, det in res.rules)
    plan = ""
    if res.entry and res.sl and res.tp:
        tp1 = res.levels.get("TP1")
        plan = (f"<div style='margin-top:10px;color:{WHITE}'>Plan: <b style='color:{BLUE}'>entry {res.entry[0]:,.0f}–{res.entry[1]:,.0f}</b>"
                f" · <b style='color:{RED}'>SL {res.sl:,.0f}</b>"
                + (f" · <b style='color:{GREEN}'>TP1 {tp1:,.0f}</b> ({res.levels.get('TP1_name', 'nearest swing')})" if tp1 else "")
                + f" · <b style='color:{GREEN}'>TP2 {res.tp:,.0f}</b> (15m external)"
                + (f" · R:R to TP2 <b>{res.rr:.1f}</b>" if res.rr else "") + "</div>")
    val = res.daily_validated
    draw_ok = res.levels.get("draw_validated")
    vnote = ("Daily-candle DIRECTION passed your NIFTY backtest — it is part of Rule 1." if val else
             "Daily-candle bias is INFO ONLY: run <code>backtest_daily_bias.py</code> on your NIFTY data first." if val is None else
             ("Daily candle: NOT a direction signal on NIFTY (failed), but its DRAW passed — PDH/PDL used as TP1."
              if draw_ok else "Daily-candle bias failed your NIFTY backtest — ignored."))
    nxt = (f"<div style='margin-top:6px;color:{AMBER}'>Next needed: <b>{res.next_needed}</b></div>"
           if res.next_needed and "TAKE" not in res.verdict else "")
    ev = fr.edge_validation()
    ecol = GREEN if ev and ev["passed"] else AMBER
    edge = (f"<div style='margin-top:8px;padding:6px 10px;border-left:3px solid {ecol};color:{ecol};font-size:0.85rem'>"
            f"{'✔' if ev and ev['passed'] else '⚠'} {fr.edge_line()}</div>")
    st.markdown(f"""<div style="background:{NAVY};border:2px solid {col};border-radius:10px;padding:14px 18px;margin:0 0 14px 0">
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
        <span style="color:{GOLD};font-weight:800">🧭 5-RULE SMC MODEL · {res.side.lower()} side · {res.score}/5</span>
        <span style="background:{col};color:#000;font-weight:900;padding:4px 14px;border-radius:6px">{res.verdict}</span></div>
      <div style="margin-top:8px">{rows}</div>{plan}{nxt}{edge}
      <div style="color:{MUTED};font-size:0.8rem;margin-top:8px">{vnote} Rule 1 uses EXTERNAL 15m structure — only swings that
      broke structure count; breaks inside the swing range are inducement. All 5 ✔ = take trade. Info only — not advice.</div>
    </div>""", unsafe_allow_html=True)


def _render_orderflow_check(desk) -> None:
    """One line: does real NIFTY 50 order flow (orderflow_recorder.py) back the desk's idea?"""
    import streamlit as st
    try:
        import nifty_orderflow as nf
        f = nf.today_flow(5)
    except Exception:                                         # noqa: BLE001
        return
    direction = 1 if desk.action == "BUY" or (desk.action != "SELL" and desk.trend_tf == "BULLISH") else \
        -1 if desk.action == "SELL" or desk.trend_tf == "BEARISH" else 0
    if f is None:
        st.caption("🌊 Order-flow check: no recording for today — start orderflow_recorder.py to see real buying/selling here.")
        return
    if direction == 0:
        st.caption(f"🌊 Order flow: {f.state['label']} · day CVD ₹{f.totals['cvd']:+,.0f} Cr")
        return
    s, why = nf.confirm(f, direction)
    col = {"CONFIRMED": GREEN, "AGAINST": RED}.get(s, MUTED)
    st.markdown(f"<div style='border-left:4px solid {col};background:{NAVY};padding:6px 12px;border-radius:4px;margin:0 0 10px 0;"
                f"color:{WHITE}'>🌊 <b>Order flow vs this {'long' if direction > 0 else 'short'} idea: "
                f"<span style='color:{col}'>{s}</span></b> — {why} · day CVD ₹{f.totals['cvd']:+,.0f} Cr "
                f"<span style='color:{AMBER};font-size:0.8rem'>(untested until backtest_orderflow.py passes)</span></div>",
                unsafe_allow_html=True)


def render(df: pd.DataFrame, df15: pd.DataFrame | None, tf: str, frames: dict | None = None) -> None:
    import streamlit as st
    rr = _rules()
    d0 = df.copy()
    if "timestamp" not in d0 and isinstance(d0.index, pd.DatetimeIndex):
        d0 = d0.reset_index().rename(columns={d0.index.name or "index": "timestamp"})
    if "closed" in d0:
        d0 = d0[d0["closed"].fillna(True).astype(bool)]
    d0 = _today(d0.reset_index(drop=True))
    if len(d0) < 20:
        st.info("SMC Trade Desk needs at least 20 closed candles.")
        return
    try:
        desk = compute(d0, df15, rr)
    except Exception as e:                                   # noqa: BLE001
        st.caption(f"SMC Trade Desk unavailable: {e}")
        return

    col = {"BUY": GREEN, "SELL": RED, "AVOID": AMBER, "WAIT": "#64748b"}[desk.action]
    tw = lambda t: {"BULLISH": "UP ▲", "BEARISH": "DOWN ▼"}.get(t, "SIDEWAYS ↔")
    plan = ""
    if desk.entry and desk.action in ("BUY", "SELL", "AVOID", "WAIT"):
        plan = (f"<div style='display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-top:10px'>"
                + "".join(f"<div style='background:{PANEL};border:1px solid {GRID};border-radius:6px;padding:8px'>"
                          f"<div style='color:{MUTED};font-size:0.75rem'>{k}</div><div style='color:{c};font-weight:700'>{v}</div></div>"
                          for k, v, c in (
                              ("ENTRY ZONE", f"{desk.entry[0]:,.0f} – {desk.entry[1]:,.0f}", BLUE),
                              ("STOP LOSS", f"{desk.sl:,.0f}" if desk.sl else "—", RED),
                              ("TARGET 1", f"{desk.tp1:,.0f}<br><span style='font-size:0.7rem;color:{MUTED}'>{desk.tp1_name}</span>" if desk.tp1 else "—", GREEN),
                              ("TARGET 2", f"{desk.tp2:,.0f}<br><span style='font-size:0.7rem;color:{MUTED}'>{desk.tp2_name}</span>" if desk.tp2 else "—", GREEN),
                              ("R : R", f"{desk.rr:.1f} : 1" if desk.rr else "—", GOLD if (desk.rr or 0) >= rr["min_rr"] else RED)))
                + "</div>")
        if desk.action in ("WAIT", "AVOID"):
            plan = (f"<div style='color:{MUTED};font-size:0.8rem;margin-top:8px'>Plan if price comes to the zone "
                    f"(not active now):</div>") + plan
    banner = (f"<div style='background:rgba(167,139,250,0.15);border:1px solid {PURPLE};border-radius:6px;padding:8px 12px;"
              f"margin-top:10px;color:{WHITE}'>🔄 <b>{desk.trend_change}</b></div>") if desk.trend_change else ""
    reasons = "".join(f"<li>{x}</li>" for x in desk.reasons)
    st.markdown(f"""<div style="background:{NAVY};border:2px solid {col};border-radius:10px;padding:14px 18px;margin:6px 0 12px 0">
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
        <div><span style="color:{GOLD};font-weight:800;letter-spacing:0.5px">🎯 SMC TRADE DESK · {tf}</span>
          <span style="color:{MUTED};margin-left:10px">15m trend <b style="color:{WHITE}">{tw(desk.trend15)}</b> ·
          {tf} trend <b style="color:{WHITE}">{tw(desk.trend_tf)}</b> · phase <b style="color:{WHITE}">{desk.phase}</b>
          {f"· range position <b style='color:{WHITE}'>{desk.position_pct * 100:.0f}%</b>" if desk.position_pct is not None else ""}</span></div>
        <span style="background:{col};color:#000;font-weight:900;font-size:1.3rem;padding:4px 18px;border-radius:6px">{desk.action}</span>
      </div>
      <div style="color:{WHITE};font-size:1.05rem;margin-top:8px"><b>{desk.headline}</b></div>
      {f"<ul style='color:#cbd5e1;margin:6px 0 0 18px'>{reasons}</ul>" if reasons else ""}
      {plan}{banner}
      {f"<div style='color:{MUTED};font-size:0.8rem;margin-top:8px'>Invalidation: {desk.invalidation}</div>" if desk.invalidation else ""}
    </div>""", unsafe_allow_html=True)

    render_five_rule(df if "timestamp" in df else d0, df15, frames)
    _render_orderflow_check(desk)

    st.plotly_chart(chart(d0, desk, f"SMC map — structure, zones, liquidity ({tf})"), use_container_width=True, key=f"smcdesk_{tf}")

    # Timeline in plain words
    ev = [(at, kind, direction, lvl) for i, at, kind, direction, lvl in desk.events
          if pd.Timestamp(at).date() == pd.to_datetime(d0["timestamp"]).iloc[-1].date()]
    if ev:
        rows = [{"Time": f"{pd.Timestamp(a):%H:%M}",
                 "Event": ("CHoCH (trend change)" if k in ("CHOCH", "MSS") else "BOS (trend continues)"),
                 "Direction": "Bullish ▲" if dr == "BULLISH" else "Bearish ▼",
                 "Level": f"{l:,.0f}",
                 "Meaning": (f"Closed {'above' if dr == 'BULLISH' else 'below'} the last swing "
                             f"{'high' if dr == 'BULLISH' else 'low'} — "
                             + ("sellers lost control" if (k in ("CHOCH", "MSS") and dr == "BULLISH") else
                                "buyers lost control" if k in ("CHOCH", "MSS") else
                                "buyers still in control" if dr == "BULLISH" else "sellers still in control"))}
                for a, k, dr, l in ev]
        with st.expander(f"📜 Today's structure story ({len(rows)} events)", expanded=False):
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    st.markdown(f"""<div style="background:{NAVY};border-left:4px solid {GOLD};border-radius:6px;padding:12px 16px;
        margin:4px 0 18px 0;color:{WHITE};font-size:0.9rem;line-height:1.5">
        <div style="color:{GOLD};font-weight:700">HOW TO READ</div>
        <ul style="margin:4px 0 0 18px;color:#cbd5e1">
        <li><b style="color:{GREEN}">BUY</b> / <b style="color:{RED}">SELL</b> = trend, location and target all agree and R:R ≥ {rr['min_rr']}. Enter only inside the blue ENTRY ZONE, SL beyond the protected swing.</li>
        <li><b style="color:{AMBER}">AVOID</b> = right trend, wrong place (buying into supply / selling into demand), or too late in the session.</li>
        <li><b>WAIT</b> = mid-range, consolidation, or 5m and 15m disagree. The plan shown is what to do IF price reaches the zone.</li>
        <li><b>CHoCH</b> = first close beyond the opposite swing → trend change warning. <b>BOS</b> = trend continuing.</li>
        <li>Amber dashed lines = untaken liquidity (targets). Grey dotted "✕ TAKEN" = already swept — never a target.</li>
        <li>Green boxes = demand (bullish OB/FVG) — buy zones. Red boxes = supply — sell zones. Amber dotted box = consolidation.</li>
        <li>Information only — not financial advice. Confirm with option chain (writers) before acting.</li></ul></div>""",
                unsafe_allow_html=True)
