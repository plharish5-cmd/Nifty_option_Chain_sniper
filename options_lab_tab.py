"""
options_lab_tab.py — 🧪 Options Lab: StockMojo-style intraday charts, built from
YOUR stored option-chain snapshots, each with a plain "how to read" panel.

Charts (all with the NIFTY line overlaid so you can see price vs positioning):
  1. Put-Call Ratio (OI PCR) intraday
  2. OI Change since yesterday — Call vs Put
  3. Total OI — Call vs Put
  4. PE − CE OI change (net writer lean)
  5. OI PCR vs Volume PCR
  6. Writer Flow per candle (our "Smart OI") + Call/Put volume per candle
  7. Vega Analysis — how much premium IV alone has added/removed (Call vs Put)
  8. Gamma Exposure by strike — walls + gamma flip
  9. IV − HV daily (options cheap or expensive vs real movement)

Every chart has three things under it:
  WHAT IT IS  ·  RIGHT NOW (auto-written from the live numbers)  ·  HOW TO READ (rules)

Plus a scoreboard at the top: each chart votes BULLISH / BEARISH / NEUTRAL.

Rules: reads stored snapshots only (no web calls), thresholds in
strategy1_rules.json → "options_lab". Information only — not financial advice.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

import config

RULES_PATH = Path(__file__).parent / "strategy1_rules.json"
_DEF = {
    "oi_multiplier": 1,               # 1 = OI as stored; set to lot size (65) to show shares like StockMojo
    "strike_band_pts": None,          # None = all strikes; e.g. 1000 = only spot ± 1000
    "pcr_bullish": 1.0, "pcr_bearish": 0.7,
    "vol_pcr_bullish": 0.8, "vol_pcr_bearish": 1.2,
    "lookback_minutes": 30,
    "vega_atm_strikes": 2,
    "gex_strikes_each_side": 10,
    "hv_window_days": 21,
    "iv_hv_expensive": 2.0, "iv_hv_cheap": -2.0,
    "smart_flow_candles": 5,
}

# ── palette (dark navy + gold, green/red for bull/bear) ───────────────────
NAVY, PANEL, GRID = "#0b1a33", "#10223f", "#1e3358"
GOLD, WHITE, MUTED = "#d4af37", "#f1f5f9", "#94a3b8"
GREEN, RED, BLUE, ORANGE, PURPLE = "#22c55e", "#ef4444", "#38bdf8", "#f59e0b", "#a78bfa"
FUT = "#9ca3af"


def _rules() -> dict:
    try:
        r = json.loads(RULES_PATH.read_text()).get("options_lab", {})
    except Exception:
        r = {}
    return {**_DEF, **{k: v for k, v in r.items() if not k.startswith("_")}}


def fmt(x: float) -> str:
    """Indian units: Cr / L / K."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "N/A"
    a, s = abs(x), "-" if x < 0 else ""
    if a < 0.5:
        return "0"
    if a >= 1e7:
        return f"{s}{a/1e7:.2f}Cr"
    if a >= 1e5:
        return f"{s}{a/1e5:.2f}L"
    if a >= 1e3:
        return f"{s}{a/1e3:.1f}K"
    return f"{s}{a:.0f}"


# ═══════════════════════════════════════════════════════════════════════════
# 1 · Compute (pure functions — testable without Streamlit)
# ═══════════════════════════════════════════════════════════════════════════
def _one_expiry(df: pd.DataFrame) -> pd.DataFrame:
    if "expiry" in df.columns and df["expiry"].nunique() > 1:
        exp = df.attrs.get("expiry")
        if exp is not None and (df["expiry"] == exp).any():
            df = df[df["expiry"] == exp]
        else:
            first = sorted(df["expiry"].dropna().unique(),
                           key=lambda e: pd.to_datetime(e, errors="coerce"))[0]
            df = df[df["expiry"] == first]
    return df.drop_duplicates("strike")


def build_series(snaps: list, r: dict | None = None) -> pd.DataFrame:
    """One row per snapshot with every intraday number the charts need."""
    r = r or _rules()
    mult = float(r["oi_multiplier"])
    band = r.get("strike_band_pts")
    k = int(r["vega_atm_strikes"])
    rows, iv_open = [], {}
    for ts, spot, df in snaps:
        if df is None or df.empty:
            continue
        d = _one_expiry(df)
        if band:
            d = d[(d["strike"] - spot).abs() <= float(band)]
        if d.empty:
            continue
        g = lambda c: float(pd.to_numeric(d[c], errors="coerce").fillna(0).sum()) if c in d else float("nan")
        ce_oi, pe_oi = g("ce_oi") * mult, g("pe_oi") * mult
        ce_chg, pe_chg = g("ce_chg_oi") * mult, g("pe_chg_oi") * mult
        ce_vol, pe_vol = g("ce_vol"), g("pe_vol")

        # Vega effect: vega × (IV now − IV at first snapshot), ATM ± k strikes
        ce_ve = pe_ve = float("nan")
        if {"ce_vega", "pe_vega", "ce_iv", "pe_iv"} <= set(d.columns):
            for _, row in d.iterrows():
                iv_open.setdefault(row["strike"], (row["ce_iv"], row["pe_iv"]))
            atm_i = (d["strike"] - spot).abs().values.argmin()
            near = d.iloc[max(0, atm_i - k): atm_i + k + 1]
            cv = pv = 0.0
            n_ok = 0
            for _, row in near.iterrows():
                o_ce, o_pe = iv_open.get(row["strike"], (np.nan, np.nan))
                vals = [row["ce_vega"], row["pe_vega"], row["ce_iv"], row["pe_iv"], o_ce, o_pe]
                if any(pd.isna(v) for v in vals):
                    continue
                cv += row["ce_vega"] * (row["ce_iv"] - o_ce)
                pv += row["pe_vega"] * (row["pe_iv"] - o_pe)
                n_ok += 1
            if n_ok:
                ce_ve, pe_ve = cv / n_ok, pv / n_ok
        atm = d.iloc[(d["strike"] - spot).abs().values.argmin()]
        rows.append(dict(
            ts=pd.Timestamp(ts), spot=float(spot),
            ce_oi=ce_oi, pe_oi=pe_oi, ce_chg=ce_chg, pe_chg=pe_chg,
            ce_vol=ce_vol, pe_vol=pe_vol,
            pcr=pe_oi / ce_oi if ce_oi else np.nan,
            vol_pcr=pe_vol / ce_vol if ce_vol else np.nan,
            pe_minus_ce=pe_chg - ce_chg,
            atm_iv=float(np.nanmean([atm.get("ce_iv", np.nan), atm.get("pe_iv", np.nan)])),
            call_vega=ce_ve, put_vega=pe_ve,
        ))
    return pd.DataFrame(rows)


def candles(s: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Price candles from snapshot spot + per-candle writer flow and volume."""
    if s.empty:
        return s
    x = s.set_index("ts")
    agg = x.resample(f"{minutes}min", origin="start_day", offset="15min").agg(
        open=("spot", "first"), high=("spot", "max"), low=("spot", "min"), close=("spot", "last"),
        ce_oi=("ce_oi", "last"), pe_oi=("pe_oi", "last"),
        ce_vol=("ce_vol", "last"), pe_vol=("pe_vol", "last")).dropna(subset=["close"])
    first = x.iloc[0]
    prev_ce = agg["ce_oi"].shift(1).fillna(first["ce_oi"])
    prev_pe = agg["pe_oi"].shift(1).fillna(first["pe_oi"])
    agg["writer_flow"] = (agg["pe_oi"] - prev_pe) - (agg["ce_oi"] - prev_ce)
    agg["call_vol"] = agg["ce_vol"].diff().fillna(agg["ce_vol"].iloc[0] - first["ce_vol"]).clip(lower=0)
    agg["put_vol"] = agg["pe_vol"].diff().fillna(agg["pe_vol"].iloc[0] - first["pe_vol"]).clip(lower=0)
    return agg.reset_index()


def gex_by_strike(df: pd.DataFrame, spot: float, r: dict | None = None) -> pd.DataFrame | None:
    """Per-strike gamma exposure (₹ Cr per 1% move). Call GEX − Put GEX (StockMojo convention)."""
    r = r or _rules()
    d = _one_expiry(df).copy()
    if not {"ce_gamma", "pe_gamma"} <= set(d.columns) or d[["ce_gamma", "pe_gamma"]].isna().all().all():
        try:
            import greeks
            exp = pd.to_datetime(df.attrs.get("expiry") or d["expiry"].iloc[0]).to_pydatetime().replace(hour=15, minute=30)
            d = greeks.add_greeks(d, spot, exp)
        except Exception:
            return None
    d = d.dropna(subset=["ce_gamma", "pe_gamma"]).sort_values("strike")
    if d.empty:
        return None
    n = int(r["gex_strikes_each_side"])
    atm_i = (d["strike"] - spot).abs().values.argmin()
    d = d.iloc[max(0, atm_i - n): atm_i + n + 1].copy()
    lot = float(getattr(config, "LOT_SIZE", 65))
    unit = spot * spot * 0.01 * lot / 1e7          # → ₹ Cr per 1% move
    d["call_gex"] = d["ce_gamma"] * d["ce_oi"] * unit
    d["put_gex"] = d["pe_gamma"] * d["pe_oi"] * unit
    d["net_gex"] = d["call_gex"] - d["put_gex"]
    d["abs_gex"] = d["call_gex"] + d["put_gex"]
    return d[["strike", "call_gex", "put_gex", "net_gex", "abs_gex"]].reset_index(drop=True)


def gamma_flip(g: pd.DataFrame) -> float | None:
    """Strike where cumulative net GEX changes sign (linear interpolation)."""
    cum = g["net_gex"].cumsum().to_numpy()
    ks = g["strike"].to_numpy(float)
    for i in range(1, len(cum)):
        if np.sign(cum[i]) != np.sign(cum[i - 1]) and cum[i] != cum[i - 1]:
            return float(ks[i - 1] + (ks[i] - ks[i - 1]) * (-cum[i - 1]) / (cum[i] - cum[i - 1]))
    return None


def iv_hv_daily(r: dict | None = None) -> pd.DataFrame | None:
    """Daily ATM IV (last snapshot of each day) minus HV (annualised, hv_window_days)."""
    r = r or _rules()
    root = Path(config.SNAPSHOT_DIR)
    if not root.exists():
        return None
    ivs = []
    for day_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        metas = sorted(day_dir.glob("*.meta.json"))
        if not metas:
            continue
        try:
            info = json.loads(metas[-1].read_text())
            df = pd.read_parquet(metas[-1].with_suffix("").with_suffix(".parquet"))
            spot = float(info["spot"])
            d = _one_expiry(df)
            atm = d.iloc[(d["strike"] - spot).abs().values.argmin()]
            iv = float(np.nanmean([atm.get("ce_iv", np.nan), atm.get("pe_iv", np.nan)]))
            ivs.append((pd.Timestamp(day_dir.name), iv, spot))
        except Exception:
            continue
    if not ivs:
        return None
    iv = pd.DataFrame(ivs, columns=["day", "iv", "spot"]).set_index("day")
    # HV from daily closes: stored daily OHLC if present, else snapshot closes
    closes = None
    for f in sorted((Path(config.DATA_DIR) / "ohlc").glob("*.csv")):
        try:
            o = pd.read_csv(f)
            dcol = "day" if "day" in o else ("date" if "date" in o else o.columns[0])
            o[dcol] = pd.to_datetime(o[dcol], errors="coerce")
            closes = o.dropna(subset=[dcol]).set_index(dcol)["close"].astype(float)
            if closes.nunique() > 5:          # ignore placeholder files
                break
            closes = None
        except Exception:
            continue
    if closes is None:
        closes = iv["spot"]
    w = int(r["hv_window_days"])
    hv = np.log(closes).diff().rolling(w, min_periods=max(5, w // 2)).std() * math.sqrt(252) * 100
    out = iv.join(hv.rename("hv"), how="left")
    out["iv_hv"] = out["iv"] - out["hv"]
    return out.reset_index()


# ═══════════════════════════════════════════════════════════════════════════
# 2 · Plain-English readings (each returns (vote, sentence))
# ═══════════════════════════════════════════════════════════════════════════
def _ago(s: pd.DataFrame, minutes: int) -> pd.Series:
    t = s["ts"].iloc[-1] - timedelta(minutes=minutes)
    prior = s[s["ts"] <= t]
    return prior.iloc[-1] if len(prior) else s.iloc[0]


def read_pcr(s, r):
    last, ago, first = s.iloc[-1], _ago(s, r["lookback_minutes"]), s.iloc[0]
    p, dp = last.pcr, last.pcr - ago.pcr
    px = last.spot - ago.spot
    lvl = ("above " + str(r["pcr_bullish"]) + " → put writers dominate (support below)" if p >= r["pcr_bullish"]
           else "below " + str(r["pcr_bearish"]) + " → call writers dominate (resistance above)" if p <= r["pcr_bearish"]
           else "in the middle → no side dominates")
    trend = "rising" if dp > 0.01 else "falling" if dp < -0.01 else "flat"
    if trend == "rising" and px > 0:
        mood = "Price up AND PCR up → rally is backed by put writers (healthy)."
    elif trend == "falling" and px < 0:
        mood = "Price down AND PCR down → fall is backed by call writers (healthy fall)."
    elif trend == "rising" and px < 0:
        mood = "Price down but PCR rising → put writers are defending the dip (possible bottom)."
    elif trend == "falling" and px > 0:
        mood = "Price up but PCR falling → call writers are selling into the rally (rally may stall)."
    else:
        mood = "No clear change in the last window."
    vote = "BULLISH" if (dp > 0.01 or p >= r["pcr_bullish"]) and not (dp < -0.01) else \
           "BEARISH" if (dp < -0.01 or p <= r["pcr_bearish"]) and not (dp > 0.01) else "NEUTRAL"
    return vote, (f"PCR **{p:.3f}** ({lvl}). Opened {first.pcr:.3f}. Last {r['lookback_minutes']} min: "
                  f"**{trend}** ({dp:+.3f}) while NIFTY moved {px:+.0f} pts. {mood}")


def read_chg(s, r):
    last, ago = s.iloc[-1], _ago(s, r["lookback_minutes"])
    dce, dpe = last.ce_chg - ago.ce_chg, last.pe_chg - ago.pe_chg
    lead = "CALL writers lead" if last.ce_chg > last.pe_chg else "PUT writers lead"
    def act(x, side):
        return f"{side} writers ADDING {fmt(x)}" if x > 0 else f"{side} writers EXITING {fmt(-x)}"
    if dce < 0 and dpe >= 0:
        story, vote = "Call writers running + put writers holding → SHORT COVERING, upside can extend.", "BULLISH"
    elif dpe < 0 and dce >= 0:
        story, vote = "Put writers running + call writers holding → LONG UNWINDING / support breaking, downside can extend.", "BEARISH"
    elif dce > 0 and dpe > 0:
        story = "Both sides adding → range being built between the two walls."
        vote = "BULLISH" if dpe > dce else "BEARISH" if dce > dpe else "NEUTRAL"
    else:
        story, vote = "Both sides exiting → positions closing, expect a quieter market.", "NEUTRAL"
    return vote, (f"Since yesterday: Call **{fmt(last.ce_chg)}**, Put **{fmt(last.pe_chg)}** → {lead} "
                  f"(gap {fmt(abs(last.ce_chg - last.pe_chg))}). Last {r['lookback_minutes']} min: "
                  f"{act(dce, 'Call')}, {act(dpe, 'Put')}. {story}")


def read_total(s, r):
    last = s.iloc[-1]
    hi_ce, hi_pe = s["ce_oi"].max(), s["pe_oi"].max()
    off_ce = (last.ce_oi - hi_ce) / hi_ce * 100 if hi_ce else 0
    off_pe = (last.pe_oi - hi_pe) / hi_pe * 100 if hi_pe else 0
    vote = "BEARISH" if last.ce_oi > last.pe_oi * 1.2 else "BULLISH" if last.pe_oi > last.ce_oi * 1.2 else "NEUTRAL"
    note = ""
    if off_ce < -5:
        note += f" Call OI is {abs(off_ce):.0f}% below its day high → call writers have been leaving (ceiling weakening)."
    if off_pe < -5:
        note += f" Put OI is {abs(off_pe):.0f}% below its day high → put writers have been leaving (floor weakening)."
    return vote, (f"Total Call OI **{fmt(last.ce_oi)}** vs Put OI **{fmt(last.pe_oi)}** "
                  f"(ratio {last.pe_oi / last.ce_oi:.2f})." + (note or " Both near their day highs — walls intact."))


def read_pe_ce(s, r):
    last, ago = s.iloc[-1], _ago(s, r["lookback_minutes"])
    v, dv = last.pe_minus_ce, last.pe_minus_ce - ago.pe_minus_ce
    side = "put-heavy (bullish lean)" if v > 0 else "call-heavy (bearish lean)"
    turn = "turning more bullish" if dv > 0 else "turning more bearish" if dv < 0 else "unchanged"
    vote = "BULLISH" if dv > 0 else "BEARISH" if dv < 0 else "NEUTRAL"
    return vote, (f"PE − CE = **{fmt(v)}** → {side}. Last {r['lookback_minutes']} min it moved {fmt(dv)} → **{turn}**. "
                  f"Day range {fmt(s['pe_minus_ce'].min())} to {fmt(s['pe_minus_ce'].max())}.")


def read_volpcr(s, r):
    last = s.iloc[-1]
    v, o = last.vol_pcr, last.pcr
    if v >= r["vol_pcr_bearish"]:
        t, vote = "heavy PUT trading today → fear / hedging (often near a short-term low)", "BEARISH"
    elif v <= r["vol_pcr_bullish"]:
        t, vote = "heavy CALL trading today → chasing upside", "BULLISH"
    else:
        t, vote = "balanced trading", "NEUTRAL"
    gap = "Volume PCR above OI PCR → today's traders more bearish than the positions held." if v > o else \
          "Volume PCR below OI PCR → today's traders more bullish than the positions held."
    return vote, f"Volume PCR **{v:.3f}** ({t}); OI PCR **{o:.3f}**. {gap}"


def read_flow(c, r):
    n = int(r["smart_flow_candles"])
    tail = c.tail(n)
    f, px = tail["writer_flow"].sum(), tail["close"].iloc[-1] - tail["open"].iloc[0]
    if f > 0 and px > 0:
        story, vote = "price up + put writing → buyers supported (bullish confirmation)", "BULLISH"
    elif f < 0 and px < 0:
        story, vote = "price down + call writing → sellers supported (bearish confirmation)", "BEARISH"
    elif f > 0 > px:
        story, vote = "price down but put writers adding → dip being defended", "BULLISH"
    elif f < 0 < px:
        story, vote = "price up but call writers adding → rally being sold", "BEARISH"
    else:
        story, vote = "no clear flow", "NEUTRAL"
    return vote, f"Last {n} candles: writer flow **{fmt(f)}**, NIFTY {px:+.0f} pts → {story}."


def read_vega(s, r):
    last = s.iloc[-1]
    cv, pv = last.call_vega, last.put_vega
    if pd.isna(cv) or pd.isna(pv):
        return "NEUTRAL", "Vega/IV not available in these snapshots."
    if pv > 0 and pv > cv:
        t, vote = "PUTS getting pricier from IV → traders paying for protection (fear)", "BEARISH"
    elif cv > 0 and cv > pv:
        t, vote = "CALLS getting pricier from IV → traders paying for upside", "BULLISH"
    elif cv < 0 and pv < 0:
        t, vote = "IV falling on both sides → calm, option buyers losing to IV crush", "NEUTRAL"
    else:
        t, vote = "mixed", "NEUTRAL"
    return vote, (f"Since open, IV alone changed ATM premium by Call **{cv:+.1f}** pts, Put **{pv:+.1f}** pts "
                  f"(ATM IV now {last.atm_iv:.1f}%). → {t}.")


def read_gex(g, spot):
    flip = gamma_flip(g)
    top_c = g.loc[g["net_gex"].idxmax()]
    top_p = g.loc[g["net_gex"].idxmin()]
    side = ""
    vote = "NEUTRAL"
    if flip:
        if spot > flip:
            side = f"Spot is ABOVE the gamma flip ({flip:,.0f}) → hedging slows moves, expect grind/pin."
        else:
            side = f"Spot is BELOW the gamma flip ({flip:,.0f}) → hedging speeds moves, expect fast swings."
    return vote, (f"Biggest call gamma (green) at **{top_c.strike:,.0f}** — price tends to be pulled to / capped at it. "
                  f"Biggest put gamma (red) at **{top_p.strike:,.0f}** — tends to act as a floor. {side}")


def read_ivhv(h, r):
    h = h.dropna(subset=["iv_hv"])
    if h.empty:
        return "NEUTRAL", "Not enough daily history yet for HV."
    v = h["iv_hv"].iloc[-1]
    pct = (h["iv_hv"] < v).mean() * 100 if len(h) > 5 else float("nan")
    if v >= r["iv_hv_expensive"]:
        t = "options EXPENSIVE vs real movement → favours option SELLERS; buyers need a big move"
    elif v <= r["iv_hv_cheap"]:
        t = "options CHEAP vs real movement → favours option BUYERS"
    else:
        t = "options fairly priced"
    p = f" ({pct:.0f}th percentile of your history)" if not math.isnan(pct) else ""
    return "NEUTRAL", f"IV − HV = **{v:+.2f}**{p}: IV {h['iv'].iloc[-1]:.1f}% vs HV {h['hv'].iloc[-1]:.1f}% → {t}."


# ═══════════════════════════════════════════════════════════════════════════
# 3 · Charts
# ═══════════════════════════════════════════════════════════════════════════
def _layout(fig, title, h=380):
    fig.update_layout(
        title=dict(text=f"<b>{title}</b>", font=dict(color=GOLD, size=16), x=0.01),
        paper_bgcolor=NAVY, plot_bgcolor=PANEL, font=dict(color=WHITE, size=12),
        height=h, margin=dict(l=10, r=10, t=50, b=10), hovermode="x unified",
        legend=dict(orientation="h", y=1.1, x=1, xanchor="right", bgcolor="rgba(0,0,0,0)", font=dict(color=WHITE, size=12)))
    fig.update_xaxes(gridcolor=GRID, showline=True, linecolor=GOLD)
    fig.update_yaxes(gridcolor=GRID, zerolinecolor=MUTED)
    return fig


def _dual(s, title, lines, y2_title=""):
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Scatter(x=s["ts"], y=s["spot"], name="NIFTY", line=dict(color=FUT, dash="dot", width=1.3)),
                  secondary_y=False)
    for col, name, color in lines:
        fig.add_trace(go.Scatter(x=s["ts"], y=s[col], name=name, line=dict(color=color, width=2.4),
                                 customdata=[fmt(v) for v in s[col]],
                                 hovertemplate=f"{name}: %{{customdata}}<extra></extra>"),
                      secondary_y=True)
    fig.update_yaxes(title_text="NIFTY", secondary_y=False, showgrid=False)
    fig.update_yaxes(title_text=y2_title, secondary_y=True)
    return _layout(fig, title)


def chart_pcr(s):
    return _dual(s, "Put-Call Ratio (OI) vs NIFTY", [("pcr", "OI PCR", BLUE)], "PCR")


def chart_chg(s):
    return _dual(s, "OI Change since yesterday — Call vs Put", [("ce_chg", "Call OI Chg", GREEN), ("pe_chg", "Put OI Chg", RED)], "OI chg")


def chart_total(s):
    return _dual(s, "Total OI — Call vs Put", [("ce_oi", "Call OI", GREEN), ("pe_oi", "Put OI", RED)], "OI")


def chart_pe_ce(s):
    fig = _dual(s, "PE − CE OI Change (net writer lean)", [("pe_minus_ce", "PE − CE", PURPLE)], "PE−CE")
    fig.add_hline(y=0, line=dict(color=MUTED, width=1), secondary_y=True)
    return fig


def chart_volpcr(s):
    return _dual(s, "OI PCR vs Volume PCR", [("pcr", "OI PCR", BLUE), ("vol_pcr", "Vol PCR", ORANGE)], "ratio")


def chart_flow(c, tf):
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, row_heights=[0.55, 0.22, 0.23], vertical_spacing=0.03)
    fig.add_trace(go.Candlestick(x=c["ts"], open=c["open"], high=c["high"], low=c["low"], close=c["close"],
                                 name="NIFTY", increasing_line_color=GREEN, decreasing_line_color=RED), 1, 1)
    fig.add_trace(go.Bar(x=c["ts"], y=c["writer_flow"], name="Smart OI",
                         marker_color=[GREEN if v >= 0 else RED for v in c["writer_flow"]],
                         customdata=[fmt(v) for v in c["writer_flow"]],
                         hovertemplate="Writer flow %{customdata}<extra></extra>"), 2, 1)
    fig.add_trace(go.Bar(x=c["ts"], y=c["call_vol"], name="Call vol", marker_color=GREEN, opacity=0.8), 3, 1)
    fig.add_trace(go.Bar(x=c["ts"], y=-c["put_vol"], name="Put vol (below 0)", marker_color=RED, opacity=0.8), 3, 1)
    fig.update_layout(xaxis_rangeslider_visible=False, barmode="relative")
    return _layout(fig, f"Smart OI (PE−CE per {tf} candle) + Call/Put volume", h=560)


def chart_vega(s):
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Scatter(x=s["ts"], y=s["spot"], name="NIFTY", line=dict(color=FUT, dash="dot", width=1.3)), secondary_y=False)
    fig.add_trace(go.Scatter(x=s["ts"], y=s["put_vega"], name="Put vega effect", fill="tozeroy",
                             line=dict(color=RED, width=2), fillcolor="rgba(239,68,68,0.18)"), secondary_y=True)
    fig.add_trace(go.Scatter(x=s["ts"], y=s["call_vega"], name="Call vega effect", fill="tozeroy",
                             line=dict(color=GREEN, width=2), fillcolor="rgba(34,197,94,0.18)"), secondary_y=True)
    fig.update_yaxes(showgrid=False, secondary_y=False)
    fig.update_yaxes(title_text="premium pts from IV", secondary_y=True)
    return _layout(fig, "Vega Analysis — premium added/removed by IV alone (ATM)")


def chart_gex(g, spot):
    flip = gamma_flip(g)
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Bar(x=g["strike"], y=g["net_gex"], name="Net GEX (Cr)",
                         marker_color=[GREEN if v >= 0 else RED for v in g["net_gex"]], opacity=0.85), secondary_y=False)
    fig.add_trace(go.Scatter(x=g["strike"], y=g["abs_gex"], name="Total GEX (Cr)", line=dict(color=BLUE, width=2.2, shape="spline")),
                  secondary_y=True)
    fig.add_vline(x=spot, line=dict(color=GOLD, dash="dash"), annotation_text=f"Spot {spot:,.0f}",
                  annotation_font_color=GOLD)
    if flip:
        fig.add_vline(x=flip, line=dict(color=PURPLE, dash="dot"), annotation_text=f"Gamma flip {flip:,.0f}",
                      annotation_position="bottom right", annotation_font_color=PURPLE)
    fig.update_layout(hovermode="x")
    fig.update_yaxes(title_text="Net GEX ₹Cr / 1%", secondary_y=False)
    fig.update_yaxes(title_text="Total", secondary_y=True, showgrid=False)
    return _layout(fig, "Gamma Exposure by strike (latest)", h=420)


def chart_ivhv(h):
    h = h.dropna(subset=["iv_hv"])
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Scatter(x=h["day"], y=h["spot"], name="NIFTY", line=dict(color=FUT, dash="dot")), secondary_y=False)
    fig.add_trace(go.Bar(x=h["day"], y=h["iv_hv"], name="IV − HV",
                         marker_color=[GREEN if v >= 0 else RED for v in h["iv_hv"]]), secondary_y=True)
    fig.update_yaxes(showgrid=False, secondary_y=False)
    return _layout(fig, "IV − HV (daily)")


# ═══════════════════════════════════════════════════════════════════════════
# 4 · "How to read" panels
# ═══════════════════════════════════════════════════════════════════════════
GUIDE = {
    "pcr": ("Put OI ÷ Call OI. Put writers sell puts when they think price will NOT fall; call writers sell calls when they think price will NOT rise.",
            ["PCR rising = more put writing = support building → bullish lean",
             "PCR falling = more call writing = resistance building → bearish lean",
             "Watch the DIRECTION with price: price up + PCR up = healthy rally; price up + PCR down = weak rally",
             "Very high (>1.5) or very low (<0.5) can mean crowded → reversal risk"]),
    "chg": ("New positions opened today (vs yesterday's close), calls vs puts. This shows who is ACTIVE today.",
            ["Green (calls) climbing = call writers adding = ceiling forming above",
             "Red (puts) climbing = put writers adding = floor forming below",
             "Green FALLING while price rises = call writers running = SHORT COVERING (strong up move)",
             "Red FALLING while price falls = put writers running = support giving way"]),
    "total": ("All open call vs put positions for this expiry. Slower than OI change — the 'weight' on each side.",
              ["The bigger side is where most writers sit (calls = ceiling, puts = floor)",
               "A line dropping off its day high = that side's writers leaving → their wall is weakening",
               "Both lines rising = range building between the walls"]),
    "pe_ce": ("Put OI change minus Call OI change — one line for the net lean of writers today.",
              ["Above 0 = put writers ahead = bullish lean; below 0 = call writers ahead = bearish lean",
               "The SLOPE matters most: line rising = turning bullish, falling = turning bearish",
               "A sharp jump back toward 0 late in the day = call writers covering (short covering)"]),
    "volpcr": ("OI PCR = positions held (slow). Volume PCR = what traded today (fast).",
               ["Volume PCR > 1.2 = heavy put buying/hedging = fear (often near short-term lows)",
                "Volume PCR < 0.8 = heavy call trading = greed / chasing",
                "Volume PCR far above OI PCR = today's traders more scared than the position holders"]),
    "flow": ("For each candle: change in Put OI minus change in Call OI. Green = put writers adding / call writers leaving. Red = the opposite.",
             ["Green bars on green candles = rally confirmed by writers",
              "Red bars on red candles = fall confirmed by writers",
              "Green bars on RED candles = dip being bought by put writers (watch for bottom)",
              "Red bars on GREEN candles = rally being sold by call writers (watch for top)"]),
    "vega": ("Vega = how much an option's price changes per 1 IV point. This line = IV change since open × vega, ATM strikes. It shows how much premium moved because of IV alone, not price.",
             ["Red (put) area rising = puts getting pricier from IV = fear / protection buying",
              "Green (call) area rising = calls getting pricier from IV = upside demand",
              "Both below 0 = IV crush → option BUYERS lose even if price is flat",
              "Put vega spiking while price falls = panic; when it starts dropping, the fall often slows"]),
    "gex": ("Gamma Exposure = gamma × OI per strike. Shows where option-seller hedging is heaviest. Green = call gamma heavier, red = put gamma heavier. Purple line = gamma flip.",
            ["Spot ABOVE the flip = sellers' hedging dampens moves → slow grind, pinning, fade extremes",
             "Spot BELOW the flip = hedging amplifies moves → fast trends, breakouts follow through",
             "Tallest green bar = magnet / cap; tallest red bar = floor",
             "Blue line (total) peaks = strikes where price tends to slow down and get stuck"]),
    "ivhv": ("IV = movement option prices expect. HV = movement NIFTY actually had (last ~1 month).",
             ["IV − HV > 0 (green) = options expensive → SELLING premium has the edge",
              "IV − HV < 0 (red) = options cheap → BUYING options has the edge",
              "Big red bars usually come in crashes, when real movement outruns IV"]),
}


def _panel(key: str, vote: str, now_text: str):
    import streamlit as st
    what, rules = GUIDE[key]
    c = {"BULLISH": GREEN, "BEARISH": RED}.get(vote, GOLD)
    li = "".join(f"<li>{x}</li>" for x in rules)
    st.markdown(
        f"""<div style="background:{NAVY};border-left:4px solid {GOLD};border-radius:6px;
        padding:12px 16px;margin:-6px 0 22px 0;color:{WHITE};font-size:0.92rem;line-height:1.5">
        <div><span style="color:{GOLD};font-weight:700">WHAT IT IS · </span>{what}</div>
        <div style="margin-top:8px"><span style="color:{GOLD};font-weight:700">RIGHT NOW · </span>
        <span style="background:{c};color:#000;font-weight:700;padding:1px 8px;border-radius:4px">{vote}</span>
        &nbsp;{_md_bold(now_text)}</div>
        <div style="margin-top:8px;color:{GOLD};font-weight:700">HOW TO READ</div>
        <ul style="margin:4px 0 0 18px;color:#cbd5e1">{li}</ul></div>""",
        unsafe_allow_html=True)


def _md_bold(t: str) -> str:
    import re
    return re.sub(r"\*\*(.+?)\*\*", rf"<b style='color:{WHITE}'>\1</b>", t)


# ═══════════════════════════════════════════════════════════════════════════
# 5 · Render
# ═══════════════════════════════════════════════════════════════════════════
def _days_available() -> list[date]:
    root = Path(config.SNAPSHOT_DIR)
    if not root.exists():
        return []
    out = []
    for p in sorted(root.iterdir(), reverse=True):
        try:
            if p.is_dir() and any(p.glob("*.meta.json")):
                out.append(datetime.strptime(p.name, "%Y-%m-%d").date())
        except ValueError:
            continue
    return out


def render() -> None:
    import streamlit as st
    import store

    r = _rules()
    st.markdown(
        f"""<div style="background:{NAVY};border-bottom:3px solid {GOLD};padding:14px 18px;border-radius:8px;margin-bottom:12px">
        <span style="color:{GOLD};font-size:1.4rem;font-weight:800">🧪 Options Lab</span>
        <span style="color:{WHITE};margin-left:12px">PCR · OI · Writer Flow · Vega · Gamma · IV−HV — from your stored snapshots</span>
        </div>""", unsafe_allow_html=True)

    days = _days_available()
    if not days:
        st.info("No option-chain snapshots stored yet. Start the scheduler during market hours.")
        return
    c1, c2, c3 = st.columns([2, 2, 3])
    day = c1.selectbox("Session", days, format_func=lambda d: d.strftime("%a %d %b %Y"), key="olab_day")
    tf = c2.radio("Candle", ["1m", "3m", "5m", "15m"], index=1, horizontal=True, key="olab_tf")

    @st.cache_data(ttl=60, show_spinner=False)
    def _load(d: date, n_files: int):
        snaps = store.load_day(d)
        return build_series(snaps, _rules()), (snaps[-1] if snaps else None)

    n_files = len(list((Path(config.SNAPSHOT_DIR) / day.strftime("%Y-%m-%d")).glob("*.meta.json")))
    with st.spinner("Reading snapshots…"):
        s, latest = _load(day, n_files)
    if s.empty or len(s) < 3 or latest is None:
        st.warning("Not enough snapshots for this session yet.")
        return
    c3.caption(f"{len(s)} snapshots · {s.ts.iloc[0]:%H:%M}–{s.ts.iloc[-1]:%H:%M} · "
               f"NIFTY {s.spot.iloc[-1]:,.1f} · OI ×{r['oi_multiplier']}")

    c = candles(s, int(tf[:-1]))
    g = gex_by_strike(latest[2], latest[1], r)
    h = iv_hv_daily(r)

    reads = {
        "pcr": read_pcr(s, r), "chg": read_chg(s, r), "total": read_total(s, r),
        "pe_ce": read_pe_ce(s, r), "volpcr": read_volpcr(s, r),
        "flow": read_flow(c, r) if len(c) else ("NEUTRAL", "No candles."),
        "vega": read_vega(s, r),
        "gex": read_gex(g, latest[1]) if g is not None else ("NEUTRAL", "Gamma not available."),
        "ivhv": read_ivhv(h, r) if h is not None else ("NEUTRAL", "No daily history."),
    }

    # Scoreboard
    names = {"pcr": "PCR", "chg": "OI Change", "total": "Total OI", "pe_ce": "PE−CE", "volpcr": "Vol PCR",
             "flow": "Smart OI", "vega": "Vega", "gex": "Gamma", "ivhv": "IV−HV"}
    bull = sum(v == "BULLISH" for v, _ in reads.values())
    bear = sum(v == "BEARISH" for v, _ in reads.values())
    overall = "BULLISH LEAN" if bull - bear >= 2 else "BEARISH LEAN" if bear - bull >= 2 else "MIXED / WAIT"
    oc = GREEN if "BULL" in overall else RED if "BEAR" in overall else GOLD
    chips = "".join(
        f"<span style='display:inline-block;margin:3px;padding:3px 10px;border-radius:12px;"
        f"background:{ {'BULLISH': GREEN, 'BEARISH': RED}.get(v, '#334155') };color:{'#000' if v != 'NEUTRAL' else WHITE};"
        f"font-size:0.85rem'>{names[k]}: {v.title()}</span>" for k, (v, _) in reads.items())
    st.markdown(
        f"""<div style="background:{NAVY};border:1px solid {GOLD};border-radius:8px;padding:12px 16px;margin-bottom:16px">
        <span style="color:{GOLD};font-weight:700">SCOREBOARD · </span>
        <span style="color:{oc};font-weight:800;font-size:1.1rem">{overall}</span>
        <span style="color:{MUTED}"> ({bull} bullish · {bear} bearish · {9 - bull - bear} neutral)</span>
        <div style="margin-top:6px">{chips}</div>
        <div style="color:{MUTED};font-size:0.8rem;margin-top:6px">A vote count, not a signal. Info only — not advice.</div>
        </div>""", unsafe_allow_html=True)

    t0, t1, t2, t3, t4, t5, t6, t7, t8, t9 = st.tabs(["📊 Open Interest", "📈 PCR & OI", "🕯️ Smart OI", "🧲 Gamma & Vega", "📅 IV − HV",
                                              "📊 Call vs Put Writers (3m / 5m / 15m)", "🟥 PE − CE OI Table",
                                              "📈 Multi-strike OI",
                                              "📉 Vega Analysis",
                                              "⏳ Premium Decay"])   # TABMOVE MULTISTRIKE VEGAPRO PREMIUMPRO
    with t0:
        try:
            import oi_profile_panel
            oi_profile_panel.render(day)
        except Exception as _oip_err:                       # noqa: BLE001
            st.error(f"Open Interest view encountered an error: {_oip_err}")
    with t1:
        try:                                                    # PCROI_PRO — PCR / OI Change / Total OI vs NIFTY future
            import pcr_oi_pro
            pcr_oi_pro.render(day)
        except Exception as _pcr_err:                           # noqa: BLE001
            st.error(f"PCR & OI vs Future encountered an error: {_pcr_err}")
        st.markdown("---")
        st.caption("Earlier PCR & OI charts (unchanged)")
        for key, fn in (("pcr", chart_pcr), ("chg", chart_chg), ("total", chart_total),
                        ("pe_ce", chart_pe_ce), ("volpcr", chart_volpcr)):
            st.plotly_chart(fn(s), use_container_width=True, key=f"olab_{key}")
            _panel(key, *reads[key])
    with t2:
        try:                                                    # SMARTOI_PRO — StockMojo-style Smart OI dashboard
            import smart_oi_pro
            smart_oi_pro.render(day)
        except Exception as _sop_err:                           # noqa: BLE001
            st.error(f"Smart OI dashboard encountered an error: {_sop_err}")
        st.markdown("---")
        st.caption("Earlier Smart OI view (unchanged)")
        if len(c):
            st.plotly_chart(chart_flow(c, tf), use_container_width=True, key="olab_flow")
        _panel("flow", *reads["flow"])
    with t3:
        if g is not None:
            st.plotly_chart(chart_gex(g, latest[1]), use_container_width=True, key="olab_gex")
        _panel("gex", *reads["gex"])
        if s["call_vega"].notna().any():
            st.plotly_chart(chart_vega(s), use_container_width=True, key="olab_vega")
        _panel("vega", *reads["vega"])
    with t4:
        if h is not None and h["iv_hv"].notna().any():
            st.plotly_chart(chart_ivhv(h), use_container_width=True, key="olab_ivhv")
        else:
            st.info("IV − HV needs at least ~10 stored days (or a real daily OHLC file in data/ohlc).")
        _panel("ivhv", *reads["ivhv"])
    with t5:                                                    # TABMOVE — moved from ⚡ Option Chain & OI Analysis
        try:
            import writers_graph_engine
            writers_graph_engine.render_writers_graph_ui(df=latest[2], spot=float(latest[1]), pa_frames=None, read=None)
        except Exception as _wge_err:                           # noqa: BLE001
            st.error(f"Call vs Put Writers Graph encountered an error: {_wge_err}")
    with t6:                                                    # TABMOVE — moved from ⚡ Option Chain & OI Analysis
        try:
            import pe_ce_panel
            pe_ce_panel.render()
        except Exception as _pece_err:                          # noqa: BLE001
            st.error(f"PE − CE OI Table encountered an error: {_pece_err}")
    with t7:                                                    # MULTISTRIKE — top-N strikes' OI through the day
        try:
            import multistrike_panel
            multistrike_panel.render(day)
        except Exception as _ms_err:                            # noqa: BLE001
            st.error(f"Multi-strike OI encountered an error: {_ms_err}")
    with t8:                                                    # VEGAPRO — StockMojo-style Vega Analysis
        try:
            import vega_pro
            vega_pro.render(day)
        except Exception as _vg_err:                            # noqa: BLE001
            st.error(f"Vega Analysis encountered an error: {_vg_err}")
    with t9:                                                    # PREMIUMPRO — StockMojo-style Premium Decay
        try:
            import premium_decay_pro
            premium_decay_pro.render(day)
        except Exception as _pd_err:                            # noqa: BLE001
            st.error(f"Premium Decay encountered an error: {_pd_err}")
