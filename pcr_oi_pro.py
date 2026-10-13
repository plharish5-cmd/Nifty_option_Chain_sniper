"""
pcr_oi_pro.py — PCR · OI Change · Total OI against the CURRENT-MONTH NIFTY FUTURE (StockMojo "Put-Call Ratio" page,
extended). Sits at the top of 🧪 Options Lab → "📈 PCR & OI"; the older charts stay underneath.

  1 · Put-Call Ratio      OI PCR (+ optional Volume PCR and Change PCR), 0.7 / 1.0 lines, day high/low marks,
                          and DAY-WISE PCR (closing PCR of the last sessions) beside it
  2 · OI Change           Call vs Put change since yesterday, gap shaded (green = puts ahead, red = calls ahead),
                          ✕ where the lead flips
  3 · Total OI            Call vs Put total, gap shaded
  On every chart: the NIFTY future (dotted, left axis), last values labelled on the right, unified hover,
  and an EXPIRY-UNWIND band on expiry afternoons (both sides closing — not a direction).

Future price, in order of preference (picked per session, caption says which was used):
  a) data/futures_1m/<day>.csv — real 1-minute candles of the CURRENT NIFTY future saved by this page
  b) Dhan /charts/intraday for the current contract (dhan_client.nifty_futures_contract), only for sessions inside
     that contract's month; saved to (a) for next time
  c) Synth future from the option chain (ATM strike + call − put) — very close to the future, always available
Information only — not financial advice.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

import options_lab_tab as ol
import pe_ce_panel as pc
from options_lab_tab import fmt, NAVY, GOLD, WHITE, MUTED, GREEN, RED, BLUE, ORANGE, PURPLE, FUT

TF = {"1m": 1, "3m": 3, "5m": 5, "15m": 15}


# ═══════════════════════════════════════════════════════════════════════════
# 1 · NIFTY future
# ═══════════════════════════════════════════════════════════════════════════
def _last_tuesday(y: int, m: int) -> date:
    d = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1))
    return d - timedelta(days=(d.weekday() - 1) % 7)


def series_start(expiry: date) -> date:
    """First session of a monthly contract's front-month life = day after the previous month's last Tuesday."""
    py, pm = (expiry.year - 1, 12) if expiry.month == 1 else (expiry.year, expiry.month - 1)
    return _last_tuesday(py, pm) + timedelta(days=1)


def _fut_dir() -> Path:
    import config
    return Path(config.DATA_DIR) / "futures_1m"


def future_1m(day: date) -> tuple[pd.DataFrame | None, str]:
    """(frame[ts, fut], label) of the current NIFTY future for `day`, or (None, why)."""
    f = _fut_dir() / f"{day:%Y-%m-%d}.csv"
    if f.exists() and day < date.today():
        try:
            x = pd.read_csv(f, parse_dates=["ts"])
            return x[["ts", "fut"]], f"NIFTY future {x['contract'].iloc[0]} (saved)"
        except Exception:                                       # noqa: BLE001
            pass
    try:
        import dhan_client as dc
        if not hasattr(dc, "nifty_futures_contract"):
            return None, "run apply_future_fix.py to get the real future — showing synth future"
        k = dc.nifty_futures_contract()
        if not k or not k.get("expiry"):
            return None, "Dhan future lookup failed (token / network?) — showing synth future"
        if not (series_start(k["expiry"]) <= day <= min(k["expiry"], date.today())):
            return None, f"{day:%d %b} was before {k['symbol']} became the front month"
        back = (date.today() - day).days + 1
        c = dc.intraday(k["id"], "NSE_FNO", "FUTIDX", days_back=back)
        c = c[c["timestamp"].dt.date == day]
        if c.empty:
            return None, "Dhan returned no future candles for that day"
        out = pd.DataFrame({"ts": c["timestamp"], "fut": c["close"].astype(float)})
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            out.assign(contract=k["symbol"], expiry=str(k["expiry"])).to_csv(f, index=False)
        except Exception:                                       # noqa: BLE001
            pass
        return out, f"NIFTY future {k['symbol']} (Dhan, expiry {k['expiry']:%d %b})"
    except Exception as exc:                                    # noqa: BLE001
        return None, f"future candles unavailable ({str(exc)[:60]})"


def synth(snaps: list) -> pd.DataFrame:
    rows = []
    for ts, spot, df in snaps:
        try:
            d = ol._one_expiry(df)
            a = d.iloc[(pd.to_numeric(d["strike"]) - spot).abs().values.argmin()]
            v = float(a["strike"]) + float(a["ce_ltp"]) - float(a["pe_ltp"])
            rows.append((pd.Timestamp(ts), v if abs(v - spot) < 400 else float(spot)))
        except Exception:                                       # noqa: BLE001
            rows.append((pd.Timestamp(ts), float(spot)))
    return pd.DataFrame(rows, columns=["ts", "fut"])


# ═══════════════════════════════════════════════════════════════════════════
# 2 · OI data
# ═══════════════════════════════════════════════════════════════════════════
def build(snaps: list, tf: str, mode: str, n: int, mult: float, fut: pd.DataFrame) -> pd.DataFrame:
    win = pc.strike_window(snaps, mode, None if mode == "All" else n, None, 50)
    s = pc.per_snapshot(snaps, win, mult)
    if s.empty:
        return s
    x = s.set_index("ts")
    m = TF[tf]
    t = x.resample(f"{m}min", origin="start_day", offset="15min").last().dropna(subset=["ce_oi"]).reset_index() if m > 1 \
        else x.reset_index()
    f = fut.set_index("ts")["fut"].sort_index()
    f = f.resample(f"{m}min", origin="start_day", offset="15min").last().dropna() if m > 1 else f
    f = f.rename("fut").reset_index()
    f["ts"] = pd.to_datetime(f["ts"]).dt.tz_localize(None).astype("datetime64[ns]")   # Dhan = µs, snapshots = ns
    t["ts"] = pd.to_datetime(t["ts"]).astype("datetime64[ns]")
    t = pd.merge_asof(t.sort_values("ts"), f.sort_values("ts"), on="ts",
                      direction="nearest", tolerance=pd.Timedelta(minutes=max(3, m)))
    t["fut"] = t["fut"].interpolate(limit_direction="both")
    t["pcr"] = t["pe_oi"] / t["ce_oi"].replace(0, np.nan)
    t["vol_pcr"] = t["pe_vol"] / t["ce_vol"].replace(0, np.nan)
    t["chg_pcr"] = t["pe_day"] / t["ce_day"].where(t["ce_day"] > 0)
    return t


def daywise(days: list, mult: float, n: int = 12) -> pd.DataFrame:
    """Closing PCR (last stored snapshot) of the last n sessions."""
    import config
    rows = []
    for d in sorted(days)[-n:]:
        folder = Path(config.SNAPSHOT_DIR) / d.strftime("%Y-%m-%d")
        metas = sorted(folder.glob("*.meta.json"))
        if not metas:
            continue
        try:
            pq = metas[-1].with_suffix("").with_suffix(".parquet")
            x = ol._one_expiry(pd.read_parquet(pq))
            ce, pe = pd.to_numeric(x["ce_oi"], errors="coerce").sum(), pd.to_numeric(x["pe_oi"], errors="coerce").sum()
            if ce:
                rows.append(dict(day=d, pcr=pe / ce, spot=float(json.loads(metas[-1].read_text())["spot"])))
        except Exception:                                       # noqa: BLE001
            continue
    return pd.DataFrame(rows)


def unwind_window(t: pd.DataFrame, expiry_day: bool) -> tuple | None:
    """Expiry afternoon where BOTH call and put total OI fall > 15 % from their day high → (start, end)."""
    if not expiry_day or len(t) < 10:
        return None
    pm = t[t["ts"].dt.hour >= 12]
    if pm.empty:
        return None
    ce_dd = 1 - pm["ce_oi"] / t["ce_oi"].cummax().loc[pm.index]
    pe_dd = 1 - pm["pe_oi"] / t["pe_oi"].cummax().loc[pm.index]
    hit = pm[(ce_dd > 0.15) & (pe_dd > 0.15)]
    if hit.empty:
        return None
    # FIX-1: walk back only while BOTH sides are falling together (15-min change < 0 on each). A stretch where calls
    # cover but puts hold is a short-covering TREND, not an unwind, and must stay unshaded.
    step = max(1, int(round(15 / max(1.0, (t["ts"].diff().dt.total_seconds().median() or 60) / 60))))
    both = (t["ce_oi"].diff(step) < 0) & (t["pe_oi"].diff(step) < 0)
    k = int(hit.index[0])
    while k - 1 in both.index and bool(both.loc[k - 1]) and t.loc[k - 1, "ts"].hour >= 12:
        k -= 1
    if not bool(both.loc[int(hit.index[0])]):
        nxt = both.loc[int(hit.index[0]):]
        nxt = nxt[nxt]
        if nxt.empty:
            return None
        k = int(nxt.index[0])
    return t.loc[k, "ts"], t["ts"].iloc[-1]


# ═══════════════════════════════════════════════════════════════════════════
# 3 · Charts
# ═══════════════════════════════════════════════════════════════════════════
def _base(t: pd.DataFrame, title: str, fut_label: str, unwind) -> go.Figure:
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Scatter(x=t["ts"], y=t["fut"], name=fut_label, line=dict(color=FUT, dash="dot", width=1.4),
                             hovertemplate="Future %{y:,.1f}<extra></extra>"), secondary_y=False)
    if unwind:
        fig.add_vrect(x0=unwind[0], x1=unwind[1], fillcolor="rgba(212,175,55,0.10)", line_width=0,
                      annotation_text="EXPIRY UNWIND — both sides closing, not a direction",
                      annotation_position="top left", annotation_font_color=GOLD)
    ol._layout(fig, title, h=430)
    fig.update_layout(hovermode="x unified", margin=dict(l=60, r=80, t=60, b=40),
                      legend=dict(orientation="h", x=1, xanchor="right", y=1.13))
    fig.update_yaxes(title_text="NIFTY future", tickformat=",.0f", secondary_y=False, showgrid=False)
    return fig


def _tag(fig, x, y, text, color):
    fig.add_annotation(x=x, y=y, yref="y2", text=f"<b>{text}</b>", showarrow=False, xanchor="left", xshift=6,
                       font=dict(color="#000", size=11), bgcolor=color, borderpad=2)


def _crr_ticks(fig, vals):
    v = pd.Series(vals, dtype=float).dropna()
    if v.empty:
        return
    ticks = np.linspace(float(v.min()), float(v.max()), 6)
    fig.update_yaxes(tickvals=ticks, ticktext=[fmt(x) for x in ticks], secondary_y=True)


def chart_pcr(t, fut_label, unwind, show_vol, show_chg):
    fig = _base(t, "Put-Call Ratio", fut_label, unwind)
    fig.add_trace(go.Scatter(x=t["ts"], y=t["pcr"], name="OI PCR", line=dict(color=BLUE, width=2.4),
                             hovertemplate="OI PCR %{y:.3f}<extra></extra>"), secondary_y=True)
    if show_vol:
        fig.add_trace(go.Scatter(x=t["ts"], y=t["vol_pcr"], name="Volume PCR", line=dict(color=ORANGE, width=1.6),
                                 hovertemplate="Vol PCR %{y:.3f}<extra></extra>"), secondary_y=True)
    if show_chg:
        fig.add_trace(go.Scatter(x=t["ts"], y=t["chg_pcr"], name="Change PCR (Δput ÷ Δcall)",
                                 line=dict(color=PURPLE, width=1.6, dash="dash"),
                                 hovertemplate="Change PCR %{y:.2f}<extra></extra>"), secondary_y=True)
    for y in (0.7, 1.0):
        fig.add_hline(y=y, line=dict(color=MUTED, width=1, dash="dot"), secondary_y=True)
    p = t["pcr"].dropna()
    if len(p):
        hi, lo = p.idxmax(), p.idxmin()
        fig.add_trace(go.Scatter(x=[t.at[hi, "ts"], t.at[lo, "ts"]], y=[p[hi], p[lo]], mode="markers+text",
                                 text=[f"high {p[hi]:.2f}", f"low {p[lo]:.2f}"], textposition=["top center", "bottom center"],
                                 marker=dict(color=GOLD, size=8), textfont=dict(color=GOLD), showlegend=False,
                                 hoverinfo="skip"), secondary_y=True)
        _tag(fig, t["ts"].iloc[-1], p.iloc[-1], f"{p.iloc[-1]:.3f}", BLUE)
    fig.update_yaxes(title_text="PCR", secondary_y=True)
    return fig


def chart_pair(t, a_col, b_col, title, fut_label, unwind):
    """Call (green) vs Put (red) with the gap shaded by who is ahead + lead-flip ✕ marks."""
    fig = _base(t, title, fut_label, unwind)
    ce, pe = t[a_col].astype(float).ffill(), t[b_col].astype(float).ffill()
    hi, lo = np.maximum(ce, pe), np.minimum(ce, pe)
    put_ahead = pe >= ce
    # shading: draw min, then max filled down to min, in two colours by segment
    for mask, col in ((put_ahead, "rgba(34,197,94,0.13)"), (~put_ahead, "rgba(239,68,68,0.13)")):
        fig.add_trace(go.Scatter(x=t["ts"], y=lo.where(mask), line=dict(width=0), showlegend=False, hoverinfo="skip",
                                 connectgaps=False), secondary_y=True)
        fig.add_trace(go.Scatter(x=t["ts"], y=hi.where(mask), line=dict(width=0), fill="tonexty", fillcolor=col,
                                 showlegend=False, hoverinfo="skip", connectgaps=False), secondary_y=True)
    fig.add_trace(go.Scatter(x=t["ts"], y=ce, name="Call", line=dict(color=GREEN, width=2.4),
                             customdata=[fmt(v) for v in ce], hovertemplate="Call %{customdata}<extra></extra>"), secondary_y=True)
    fig.add_trace(go.Scatter(x=t["ts"], y=pe, name="Put", line=dict(color=RED, width=2.4),
                             customdata=[fmt(v) for v in pe], hovertemplate="Put %{customdata}<extra></extra>"), secondary_y=True)
    gap = pe - ce
    fig.add_trace(go.Scatter(x=t["ts"], y=gap, name="Put − Call", visible="legendonly",
                             line=dict(color=PURPLE, width=1.4, dash="dash"),
                             customdata=[fmt(v) for v in gap], hovertemplate="Put − Call %{customdata}<extra></extra>"),
                  secondary_y=True)
    # lead flips with hysteresis: counted only once the new leader is ahead by > 5 % of the day's widest gap
    band = 0.05 * float(gap.abs().max() or 0)
    idx, side = [], 0
    for i, g in zip(t.index, gap):
        now = 1 if g > band else -1 if g < -band else 0
        if now and side and now != side:
            idx.append(i)
        if now:
            side = now
    flips = t.loc[idx]
    if len(flips):
        fig.add_trace(go.Scatter(x=flips["ts"], y=pe.loc[flips.index], mode="markers", name="Lead flipped",
                                 marker=dict(symbol="x", size=10, color=GOLD),
                                 hovertemplate="Lead changed hands<extra></extra>"), secondary_y=True)
    _tag(fig, t["ts"].iloc[-1], pe.iloc[-1], fmt(pe.iloc[-1]), RED)
    _tag(fig, t["ts"].iloc[-1], ce.iloc[-1], fmt(ce.iloc[-1]), GREEN)
    _crr_ticks(fig, pd.concat([ce, pe]))
    return fig


def flow_frame(t: pd.DataFrame, minutes: int = 30) -> pd.DataFrame:
    """Per candle: Δput − Δcall (writer flow), 30-min writers balance and its regime (olab_pro rules)."""
    import olab_pro as op
    f = pd.DataFrame({"ts": t["ts"], "spot": t["fut"], "ce": t["ce_oi"].astype(float), "pe": t["pe_oi"].astype(float)})
    b = op.regimes(op.balance(f, minutes))
    b["flow"] = f["pe"].diff().fillna(0).values - f["ce"].diff().fillna(0).values
    a = b["flow"].abs()
    base = a.shift(1).expanding(min_periods=6).median()
    b["sig"] = (a >= 2.5 * base) & (a > 0)
    return b


def chart_pair_flow(t, a_col, b_col, title, fut_label, unwind):
    try:
        return _chart_pair_flow(t, a_col, b_col, title, fut_label, unwind)
    except ImportError:                                         # olab_pro.py missing → previous chart
        return chart_pair(t, a_col, b_col, title, fut_label, unwind)


def _chart_pair_flow(t, a_col, b_col, title, fut_label, unwind):
    """Call vs Put lines (gap shaded) + a FLOW strip underneath: per-candle Δput − Δcall bars, 30-min writers balance,
    regime bands, ▲▼ where the balance flips (trend-change alarm) and ◆ on unusually large writer candles."""
    import olab_pro as op
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.66, 0.34], vertical_spacing=0.05,
                        specs=[[{"secondary_y": True}], [{"secondary_y": True}]])
    fig.add_trace(go.Scatter(x=t["ts"], y=t["fut"], name=fut_label, line=dict(color=FUT, dash="dot", width=1.4),
                             hovertemplate="Future %{y:,.1f}<extra></extra>"), 1, 1, secondary_y=False)
    ce, pe = t[a_col].astype(float).ffill(), t[b_col].astype(float).ffill()
    hi, lo = np.maximum(ce, pe), np.minimum(ce, pe)
    put_ahead = pe >= ce
    for mask, col in ((put_ahead, "rgba(34,197,94,0.13)"), (~put_ahead, "rgba(239,68,68,0.13)")):
        fig.add_trace(go.Scatter(x=t["ts"], y=lo.where(mask), line=dict(width=0), showlegend=False, hoverinfo="skip"),
                      1, 1, secondary_y=True)
        fig.add_trace(go.Scatter(x=t["ts"], y=hi.where(mask), line=dict(width=0), fill="tonexty", fillcolor=col,
                                 showlegend=False, hoverinfo="skip"), 1, 1, secondary_y=True)
    fig.add_trace(go.Scatter(x=t["ts"], y=ce, name="Call", line=dict(color=GREEN, width=2.4),
                             customdata=[fmt(v) for v in ce], hovertemplate="Call %{customdata}<extra></extra>"),
                  1, 1, secondary_y=True)
    fig.add_trace(go.Scatter(x=t["ts"], y=pe, name="Put", line=dict(color=RED, width=2.4),
                             customdata=[fmt(v) for v in pe], hovertemplate="Put %{customdata}<extra></extra>"),
                  1, 1, secondary_y=True)
    b = flow_frame(t)
    # flow strip
    fig.add_trace(go.Bar(x=b["ts"], y=b["flow"], name="Writer flow / candle (Δput − Δcall)",
                         marker_color=["rgba(34,197,94,.8)" if v >= 0 else "rgba(239,68,68,.8)" for v in b["flow"]],
                         customdata=[fmt(v) for v in b["flow"]], hovertemplate="flow %{customdata}<extra></extra>"),
                  2, 1, secondary_y=False)
    fig.add_trace(go.Scatter(x=b["ts"], y=b["bal"], name="Writers balance 30m", line=dict(color=GOLD, width=2),
                             hovertemplate="balance %{y:+.2f}<extra></extra>"), 2, 1, secondary_y=True)
    for y in (op.BAL_ON, -op.BAL_ON):
        fig.add_hline(y=y, line=dict(color=MUTED, dash="dot", width=1), row=2, col=1, secondary_y=True)
    # regime bands over both rows
    cur, start = None, None
    for ts, r in zip(list(b["ts"]) + [None], list(b["regime"]) + [None]):
        if r != cur:
            if cur in ("BULL", "BEAR") and start is not None:
                fig.add_vrect(x0=start, x1=ts or b["ts"].iloc[-1], line_width=0, layer="below",
                              fillcolor="rgba(34,197,94,.07)" if cur == "BULL" else "rgba(239,68,68,.07)")
            cur, start = r, ts
    fl = b[b["tflip"]]
    if len(fl):
        y = t.set_index("ts")["fut"].reindex(fl["ts"]).values
        fig.add_trace(go.Scatter(x=fl["ts"], y=y, mode="markers+text", name="Trend flip (writers)",
                                 text=["▲ BULL" if r == "BULL" else "▼ BEAR" for r in fl["regime_confirmed"]],
                                 textposition=["bottom center" if r == "BULL" else "top center" for r in fl["regime_confirmed"]],
                                 textfont=dict(color=[GREEN if r == "BULL" else RED for r in fl["regime_confirmed"]], size=12),
                                 marker=dict(symbol=["triangle-up" if r == "BULL" else "triangle-down" for r in fl["regime_confirmed"]],
                                             size=13, color=[GREEN if r == "BULL" else RED for r in fl["regime_confirmed"]],
                                             line=dict(color=WHITE, width=1)),
                                 hovertemplate="%{x|%H:%M} writers flipped → %{text}<extra></extra>"), 1, 1, secondary_y=False)
    sg = b[b["sig"]]
    if len(sg):
        fig.add_trace(go.Scatter(x=sg["ts"], y=sg["flow"], mode="markers", name="Unusual writer candle (≥ 2.5× normal)",
                                 marker=dict(symbol="diamond", size=9, color=GOLD, line=dict(color=WHITE, width=1)),
                                 customdata=[fmt(v) for v in sg["flow"]],
                                 hovertemplate="unusual flow %{customdata}<extra></extra>"), 2, 1, secondary_y=False)
    if unwind:
        fig.add_vrect(x0=unwind[0], x1=unwind[1], fillcolor="rgba(212,175,55,0.10)", line_width=0,
                      annotation_text="EXPIRY UNWIND — both sides closing, not a direction",
                      annotation_position="top left", annotation_font_color=GOLD)
    ol._layout(fig, title, h=620)
    fig.update_layout(hovermode="x unified", margin=dict(l=60, r=80, t=60, b=40), bargap=0.15,
                      legend=dict(orientation="h", x=1, xanchor="right", y=1.1))
    fig.update_yaxes(title_text="NIFTY future", tickformat=",.0f", showgrid=False, row=1, col=1, secondary_y=False)
    v = pd.concat([ce, pe]).dropna()
    if len(v):
        ticks = np.linspace(float(v.min()), float(v.max()), 6)
        fig.update_yaxes(tickvals=ticks, ticktext=[fmt(x) for x in ticks], row=1, col=1, secondary_y=True)
    fv = b["flow"].dropna()
    if len(fv) and fv.abs().max() > 0:
        m = float(fv.abs().max())
        ticks = np.linspace(-m, m, 5)
        fig.update_yaxes(tickvals=ticks, ticktext=[fmt(x) for x in ticks], title_text="flow", row=2, col=1, secondary_y=False)
    fig.update_yaxes(range=[-1.05, 1.05], tickvals=[-1, -0.25, 0, 0.25, 1], title_text="balance", showgrid=False,
                     row=2, col=1, secondary_y=True)
    gap = pe - ce                                               # lead flips of the two lines (hysteresis 5 %)
    band = 0.05 * float(gap.abs().max() or 0)
    idx, side_ = [], 0
    for i, g_ in zip(t.index, gap):
        nw = 1 if g_ > band else -1 if g_ < -band else 0
        if nw and side_ and nw != side_:
            idx.append(i)
        if nw:
            side_ = nw
    if idx:
        fig.add_trace(go.Scatter(x=t.loc[idx, "ts"], y=pe.loc[idx], mode="markers", name="Lines crossed (lead flipped)",
                                 marker=dict(symbol="x", size=10, color=GOLD),
                                 hovertemplate="Put / Call lines crossed<extra></extra>"), 1, 1, secondary_y=True)
    _tag(fig, t["ts"].iloc[-1], pe.iloc[-1], fmt(pe.iloc[-1]), RED)
    _tag(fig, t["ts"].iloc[-1], ce.iloc[-1], fmt(ce.iloc[-1]), GREEN)
    return fig


def flow_note(t: pd.DataFrame) -> str:
    b = flow_frame(t)
    l = b.iloc[-1]
    fl = b[b["tflip"]]
    lab = {"BULL": "PUT writers lead (bullish)", "BEAR": "CALL writers lead (bearish)", "UNWIND": "both sides unwinding",
           "BAL": "balanced — nobody in control"}[l["regime"]]
    last = (f" Last trend flip {fl['ts'].iloc[-1]:%H:%M} → {'▲ BULL' if fl['regime_confirmed'].iloc[-1] == 'BULL' else '▼ BEAR'}."
            if len(fl) else " No trend flip today.")
    n_sig = int(b["sig"].tail(10).sum())
    return (f" <b>Flow strip:</b> 30-min writers balance {l['bal']:+.2f} → {lab}.{last}"
            + (f" {n_sig} unusual writer candle(s) in the last 10 — someone big is positioning." if n_sig else ""))


def chart_daywise(dw: pd.DataFrame) -> go.Figure:
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    cols = [GREEN if v >= 1 else RED if v < 0.7 else GOLD for v in dw["pcr"]]
    fig.add_trace(go.Bar(x=[d.strftime("%d %b") for d in dw["day"]], y=dw["pcr"], marker_color=cols, name="Closing PCR",
                         text=[f"{v:.2f}" for v in dw["pcr"]], textposition="outside",
                         hovertemplate="%{x}: PCR %{y:.2f}<extra></extra>"), secondary_y=False)
    fig.add_trace(go.Scatter(x=[d.strftime("%d %b") for d in dw["day"]], y=dw["spot"], name="NIFTY close",
                             line=dict(color=FUT, dash="dot"), hovertemplate="NIFTY %{y:,.0f}<extra></extra>"), secondary_y=True)
    fig.add_hline(y=1.0, line=dict(color=MUTED, dash="dot"))
    ol._layout(fig, "Day-wise PCR (closing)", h=430)
    fig.update_layout(legend=dict(orientation="h", x=0, xanchor="left", y=1.12), margin=dict(l=40, r=50, t=60, b=40))
    fig.update_yaxes(showgrid=False, tickformat=",.0f", secondary_y=True)
    return fig


# ═══════════════════════════════════════════════════════════════════════════
# 4 · Reads
# ═══════════════════════════════════════════════════════════════════════════
def reads(t: pd.DataFrame, unwind, n: int = 15) -> dict:
    l, f = t.iloc[-1], t.iloc[0]
    k = t.iloc[max(0, len(t) - n)]
    fpx, rpx = float(l.fut - f.fut), float(l.fut - k.fut)
    p_now, p_rec = float(l.pcr), float(l.pcr - k.pcr)
    zone = "put-heavy, above 1" if p_now >= 1 else "call-heavy, below 0.7" if p_now < 0.7 else "middle zone"
    same = (p_rec > 0) == (rpx > 0)
    pcr = (f"PCR {p_now:.2f} ({zone}); {p_now - float(f.pcr):+.2f} since open, {p_rec:+.2f} in the last {n} candles while "
           f"the future moved {rpx:+.0f} pts → " + ("PCR moving WITH price — writers support the move." if same and abs(p_rec) > 0.01
                                                 else "PCR moving AGAINST price — the move lacks writer support." if abs(p_rec) > 0.01
                                                 else "PCR flat."))
    dce, dpe = float(l.ce_day - k.ce_day), float(l.pe_day - k.pe_day)
    if dpe > 0 and dce < 0:
        st = "puts being written, calls covered → bullish build"
    elif dce > 0 and dpe < 0:
        st = "calls being written, puts covered → bearish build"
    elif dce > 0 and dpe > 0:
        st = "both sides writing → range being set"
    elif dce < 0 and dpe < 0:
        st = "both sides closing → unwinding (expiry / EOD), no direction"
    else:
        st = "little change"
    chg = (f"Since yesterday: Put {fmt(l.pe_day)} vs Call {fmt(l.ce_day)} (gap {fmt(l.pe_day - l.ce_day)}). "
           f"Last {n} candles: Put {fmt(dpe)}, Call {fmt(dce)} → {st}.")
    tot = (f"Total OI: Put {fmt(l.pe_oi)} vs Call {fmt(l.ce_oi)}; "
           f"put {100 * (l.pe_oi / f.pe_oi - 1):+.1f}% and call {100 * (l.ce_oi / f.ce_oi - 1):+.1f}% since open. "
           + ("Expiry unwind in progress — ignore direction from OI now." if unwind else
              "Bigger side = where most money is parked; its strike walls matter most."))
    return dict(pcr=pcr, chg=chg, tot=tot, fut=fpx)


HOW = {
    "pcr": ["<b>OI PCR</b> = total put OI ÷ total call OI. Above 1 = more puts written (writers expect support); "
            "below 0.7 = more calls written (writers expect a cap).",
            "Read the <b>direction with the future</b>: PCR rising while the future rises = puts written into the rally "
            "(healthy). PCR falling while the future rises = calls being added on the way up (rally running into supply).",
            "<b>Volume PCR</b> (orange) = today's trading. Far above OI PCR = heavy put buying (hedging / fear).",
            "<b>Change PCR</b> (purple) = today's NEW puts ÷ NEW calls — reacts faster than OI PCR.",
            "<b>Day-wise PCR</b> (right) shows the bigger picture: one day's PCR means little; a 5-day climb or fall matters more.",
            "Our backtests found PCR alone has NO edge — use it to confirm price at your levels, not as a trigger."],
    "chg": ["Change since yesterday's close for all calls (green) and puts (red) of this expiry.",
            "Green shade = put writers ahead (bullish lean). Red shade = call writers ahead (bearish lean). Gold ✕ = lead flipped.",
            "Put line rising with the future = floor being built under the move. Call line rising as the future falls = ceiling pressing down.",
            "BOTH lines falling together (gold band on expiry afternoons) = positions closing — not a buy or sell."],
    "tot": ["Total open OI — the stock of positions, slower than OI Change.",
            "The gap between the lines is the PCR in money terms. A widening gap confirms the lean; a narrowing gap = the leader is closing out.",
            "On expiry day both fall into the close (contracts expire). Use the NEXT expiry's chain for tomorrow's levels."],
}


HOW_FLOW = ["<b>Flow strip (bottom):</b> each bar = puts added − calls added in that candle. Green = put writers "
            "stronger, red = call writers stronger. ◆ = a candle ≥ 2.5× the day's normal size: someone big positioning.",
            "<b>Gold line</b> = 30-min writers balance (−1 only calls … +1 only puts). Above the upper dotted line = bullish "
            "regime (green band), below the lower = bearish (red band).",
            "<b>▲ BULL / ▼ BEAR on the future line</b> = the balance flipped: the trend-change alarm. Experienced readers "
            "do NOT trade against the latest flip, and take the first pullback after it.",
            "A flip that reverses within ~15 min is noise. Flips during an expiry-afternoon unwind band are not direction."]


def _box(title, read, how):
    li = "".join(f"<li style='margin:2px 0'>{x}</li>" for x in how)
    return (f"<div style='background:{NAVY};border:1px solid {GOLD};border-radius:8px;padding:10px 14px;margin:4px 0 14px'>"
            f"<div style='color:{GOLD};font-weight:700'>{title}</div><div style='color:{WHITE};margin:4px 0 6px'>{read}</div>"
            f"<details><summary style='color:{GOLD};cursor:pointer'>How to read this chart</summary>"
            f"<ul style='color:{WHITE};margin:6px 0 0 18px'>{li}</ul></details></div>")


# ═══════════════════════════════════════════════════════════════════════════
# 5 · Streamlit
# ═══════════════════════════════════════════════════════════════════════════
def render(day: date | None = None) -> None:
    import streamlit as st
    import store
    import config

    days = ol._days_available()
    if not days:
        st.info("No option-chain snapshots stored yet.")
        return
    c = st.columns([1.5, 1.6, 1.5, 0.9, 1.7, 1.8])
    day = c[0].selectbox("Session", days, index=days.index(day) if day in days else 0,
                         format_func=lambda d: d.strftime("%a %d %b %Y"), key="pcrp_day")
    tf = c[1].radio("Timeframe", list(TF), index=0, horizontal=True, key="pcrp_tf")
    mode = c[2].radio("Strikes", ["All", "Auto ATM"], horizontal=True, key="pcrp_mode")
    n = c[3].selectbox("± Strikes", [5, 10, 15, 20], index=1, key="pcrp_n", disabled=(mode == "All"))
    src = c[4].radio("Future line", ["NIFTY future", "Synth future"], horizontal=True, key="pcrp_src",
                     help="NIFTY future = real 1-minute candles of the CURRENT month contract (Dhan). "
                          "Synth = ATM strike + call − put from your chain (used automatically if the real one is unavailable).")
    unit = c[5].radio("Units", ["Quantity (×lot)", "Contracts"], horizontal=True, key="pcrp_unit",
                      help="Quantity = contracts × lot size — same numbers as StockMojo (Cr).")
    o = st.columns([1, 1, 4])
    show_vol = o[0].checkbox("Volume PCR", value=True, key="pcrp_vol")
    show_chg = o[1].checkbox("Change PCR", value=False, key="pcrp_chg")

    lot = float(getattr(config, "LOT_SIZE", 65) or 65)
    mult = lot if unit.startswith("Quantity") else 1.0

    @st.cache_data(ttl=60, show_spinner=False)
    def _snaps(d: date, n_files: int):
        return store.load_day(d)

    @st.cache_data(ttl=60, show_spinner=False)
    def _fut(d: date, n_files: int):
        return future_1m(d)

    @st.cache_data(ttl=600, show_spinner=False)
    def _dw(ds: tuple, m: float):
        return daywise(list(ds), m)

    n_files = len(list((Path(config.SNAPSHOT_DIR) / day.strftime("%Y-%m-%d")).glob("*.meta.json")))
    snaps = _snaps(day, n_files)
    if len(snaps) < 3:
        st.warning("Not enough snapshots for this session yet.")
        return
    fut, why = (_fut(day, n_files) if src == "NIFTY future" else (None, "Synth future chosen"))
    if fut is None or len(fut) < 3:
        fut, label, note = synth(snaps), "Synth future", why
    else:
        label, note = "NIFTY future", why
    t = build(snaps, tf, mode, n, mult, fut)
    if t.empty:
        st.warning("No strikes inside that range.")
        return
    try:
        exp = pd.to_datetime(ol._one_expiry(snaps[-1][2])["expiry"].iloc[0], errors="coerce").date()
    except Exception:                                           # noqa: BLE001
        exp = None
    expiry_day = exp == day
    uw = unwind_window(t, expiry_day)
    dte = f"{(exp - day).days}d" if exp else "?"
    st.caption(f"Option expiry {exp:%d %b %Y} ({dte}) · {len(t)} candles · strikes: "
               f"{'all' if mode == 'All' else f'ATM ±{n}'} · units: {unit} · future line: {note}")

    rd = reads(t, uw)
    left, right = st.columns([3, 1])
    left.plotly_chart(chart_pcr(t, label, uw, show_vol, show_chg), use_container_width=True, key="pcrp_pcr")
    dw = _dw(tuple(days), mult)
    if len(dw) >= 2:
        right.plotly_chart(chart_daywise(dw), use_container_width=True, key="pcrp_dw")
    st.markdown(_box("PUT-CALL RATIO — now", rd["pcr"], HOW["pcr"]), unsafe_allow_html=True)
    st.plotly_chart(chart_pair_flow(t, "ce_day", "pe_day", "OI Change (Call vs Put) — since yesterday", label, uw),
                    use_container_width=True, key="pcrp_chg_chart")
    try:
        fnote = flow_note(t)
    except Exception:                                           # noqa: BLE001
        fnote = ""
    st.markdown(_box("OI CHANGE — now", rd["chg"] + fnote, HOW["chg"] + HOW_FLOW), unsafe_allow_html=True)
    st.plotly_chart(chart_pair_flow(t, "ce_oi", "pe_oi", "Total OI (Call vs Put)", label, uw),
                    use_container_width=True, key="pcrp_tot")
    st.markdown(_box("TOTAL OI — now", rd["tot"], HOW["tot"]), unsafe_allow_html=True)
    st.caption("Information only — not a trading signal.")
