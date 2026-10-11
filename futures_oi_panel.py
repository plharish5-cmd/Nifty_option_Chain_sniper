"""
futures_oi_panel.py — 📈 Futures OI · EMA 8 (NIFTY + BANKNIFTY) for 🧪 Options Lab.

Same rules as the ICT / SMC desk (port 8600), so both screens agree:
  * Build-up per candle (current-month future, price + OI):
        price ↑ OI ↑ LONG BUILD-UP · price ↓ OI ↑ SHORT BUILD-UP · price ↑ OI ↓ SHORT COVERING · price ↓ OI ↓ LONG UNWINDING
    counted only if |ΔOI| ≥ 0.05 % of OI and |Δprice| ≥ 0.1 × ATR(14), else "flat".
  * OI line with its 8-EMA. OI SURGE = ΔOI ≥ 2.5 × the average |ΔOI| of the previous 20 candles AND OI above its
    8-EMA → positions are being added unusually fast: a big move is building. Direction is NOT implied — the
    build-up type and your structure decide that.
  * NIFTY vs BANKNIFTY: both on the same side = broad move; opposite sides = narrow move, more likely to fail.

Data: Dhan /charts/intraday with oi=True for the two futures (2 Data-API calls per refresh, cached 60 s).
Contracts come from Dhan's scrip master and are PRICE-CHECKED against their index (a name match alone once gave a
"NIFTY future" trading at 1,580). Information only — not advice.
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime
from typing import Optional

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

NAVY, PANEL, GRID = "#0b1a33", "#10223f", "#1e3358"
GOLD, WHITE, MUTED = "#d4af37", "#f1f5f9", "#94a3b8"
GREEN, RED, BLUE, ORANGE = "#22c55e", "#ef4444", "#38bdf8", "#f59e0b"
BUILD_CLR = {"long build-up": GREEN, "short covering": "#86efac", "short build-up": RED,
             "long unwinding": "#fca5a5", "flat": "#475569"}
BIAS = {"long build-up": "bull", "short covering": "bull", "short build-up": "bear", "long unwinding": "bear"}

R = {"oi_min_pct": 0.05, "price_min_atr": 0.1, "ema_len": 8, "surge_k": 2.5, "surge_window": 20, "summary_bars": 12,
     "max_basis_pct": 3.0}


# ── pure functions (tested without Streamlit / Dhan) ─────────────────────────
def resample(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """1-minute rows (ts, o, h, l, c, v, oi) → minutes-candles aligned to 09:15. OI = value at the candle's last minute."""
    if df.empty:
        return df
    x = df.set_index("ts").sort_index()
    if minutes == 1:
        out = x.copy()
    else:
        g = x.resample(f"{minutes}min", label="left", closed="left")
        out = pd.DataFrame({"o": g["o"].first(), "h": g["h"].max(), "l": g["l"].min(), "c": g["c"].last(),
                            "v": g["v"].sum(), "oi": g["oi"].last()}).dropna(subset=["c"])
    return out.reset_index()


def analyse(bars: pd.DataFrame, r: dict = R) -> pd.DataFrame:
    """Adds doi, build, ema, surge, surge_x. Uses only earlier candles for every value (no look-ahead)."""
    b = bars.copy().reset_index(drop=True)
    if b.empty:
        return b
    pc = b["c"].shift(1)
    tr = pd.concat([b["h"] - b["l"], (b["h"] - pc).abs(), (b["l"] - pc).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
    b["doi"] = b["oi"].diff()
    dp = b["c"].diff()
    builds = []
    for i in range(len(b)):
        if i == 0 or pd.isna(b.at[i, "doi"]) or pd.isna(b.at[i - 1, "oi"]) or not b.at[i - 1, "oi"]:
            builds.append(None)
            continue
        doi, d = b.at[i, "doi"], dp.iat[i]
        if abs(doi) >= r["oi_min_pct"] / 100 * b.at[i - 1, "oi"] and abs(d) >= r["price_min_atr"] * atr.iat[i]:
            builds.append({(True, True): "long build-up", (False, True): "short build-up",
                           (True, False): "short covering", (False, False): "long unwinding"}[(d > 0, doi > 0)])
        else:
            builds.append("flat")
    b["build"] = builds
    b["ema"] = b["oi"].ewm(span=r["ema_len"], adjust=False).mean()
    base = b["doi"].abs().shift(1).rolling(r["surge_window"], min_periods=5).mean()
    b["surge_x"] = (b["doi"] / base).round(1)
    b["surge"] = (b["doi"] > 0) & (b["doi"] >= r["surge_k"] * base) & (b["oi"] > b["ema"])
    return b


def summary(b: pd.DataFrame, r: dict = R) -> Optional[dict]:
    t = b.dropna(subset=["build"]).tail(r["summary_bars"])
    if t.empty:
        return None
    counts = t[t["build"] != "flat"]["build"].value_counts()
    dom = counts.index[0] if len(counts) else "flat"
    last_surge = b[b["surge"]].tail(1)
    return {"dominant": dom, "bias": BIAS.get(dom), "net_doi": int(t["doi"].sum()), "bars": len(t),
            "last_build": t["build"].iat[-1], "oi": int(b["oi"].iat[-1]),
            "surge": None if last_surge.empty else {"ts": last_surge["ts"].iat[0], "x": float(last_surge["surge_x"].iat[0])}}


def compare(n: Optional[dict], k: Optional[dict]) -> Optional[dict]:
    if not n or not k:
        return None
    if n["bias"] and k["bias"] and n["bias"] == k["bias"]:
        return {"verdict": "ALIGNED", "text": f"broad {'buying' if n['bias'] == 'bull' else 'selling'} — NIFTY {n['dominant']}, "
                                               f"BANKNIFTY {k['dominant']}. Higher confidence in the move."}
    if n["bias"] and k["bias"]:
        return {"verdict": "DIVERGING", "text": f"narrow move — NIFTY {n['dominant']} but BANKNIFTY {k['dominant']}. "
                                                 f"More likely to fail; this is where traps (CRT / turtle soup) work."}
    return {"verdict": "UNCLEAR", "text": "one side is flat — no confirmation either way."}


def figure(b: pd.DataFrame, name: str, tf: str) -> go.Figure:
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.68, 0.32], vertical_spacing=0.04,
                        specs=[[{"secondary_y": True}], [{}]])
    fig.add_trace(go.Scatter(x=b["ts"], y=b["oi"], name=f"{name} FUT OI", line=dict(color=BLUE, width=2)), 1, 1)
    fig.add_trace(go.Scatter(x=b["ts"], y=b["ema"], name="OI EMA 8", line=dict(color=GOLD, width=1.3)), 1, 1)
    fig.add_trace(go.Scatter(x=b["ts"], y=b["c"], name=f"{name} FUT price", line=dict(color="#9ca3af", width=1, dash="dot")),
                  1, 1, secondary_y=True)
    s = b[b["surge"]]
    if len(s):
        fig.add_trace(go.Scatter(x=s["ts"], y=s["oi"], mode="markers+text", name="OI surge", text=[f"⚡{x}×" for x in s["surge_x"]],
                                 textposition="top center", marker=dict(color=ORANGE, size=9, symbol="diamond"),
                                 textfont=dict(color=ORANGE, size=10)), 1, 1)
    colors = [ORANGE if sg else BUILD_CLR.get(bd or "flat", "#475569") for sg, bd in zip(b["surge"], b["build"])]
    fig.add_trace(go.Bar(x=b["ts"], y=b["doi"], name="ΔOI (colour = build-up)", marker_color=colors,
                         hovertext=b["build"].fillna(""), hoverinfo="x+y+text"), 2, 1)
    fig.update_layout(height=470, paper_bgcolor=NAVY, plot_bgcolor=PANEL, font=dict(color=WHITE),
                      title=dict(text=f"{name} futures OI · EMA 8 · ΔOI ({tf})", font=dict(color=GOLD)),
                      legend=dict(orientation="h", y=1.08, bgcolor="rgba(0,0,0,0)", font=dict(color=WHITE)), margin=dict(l=50, r=50, t=70, b=30),
                      bargap=0.15)
    for ax in ("xaxis", "xaxis2", "yaxis", "yaxis2", "yaxis3"):
        fig.update_layout(**{ax: dict(gridcolor=GRID, zerolinecolor=GRID)})
    fig.update_yaxes(title_text="OI", row=1, col=1, secondary_y=False)
    fig.update_yaxes(title_text="price", row=1, col=1, secondary_y=True, showgrid=False, tickformat=",.0f")
    fig.update_yaxes(tickformat=".3s", row=1, col=1, secondary_y=False)
    fig.update_yaxes(title_text="ΔOI", row=2, col=1)
    return fig


# ── Dhan (only called from render) ───────────────────────────────────────────
def _payload_to_df(p: dict) -> pd.DataFrame:
    d = p.get("data", p) if isinstance(p, dict) else {}
    ts = d.get("timestamp") or d.get("start_Time") or []
    if not ts:
        return pd.DataFrame(columns=["ts", "o", "h", "l", "c", "v", "oi"])
    n = len(ts)
    df = pd.DataFrame({"ts": pd.to_datetime(ts, unit="s").tz_localize("UTC").tz_convert("Asia/Kolkata").tz_localize(None),
                       "o": d.get("open"), "h": d.get("high"), "l": d.get("low"), "c": d.get("close"),
                       "v": d.get("volume") or [0] * n, "oi": d.get("open_interest") or [np.nan] * n})
    t = df["ts"].dt.time
    return df[(t >= dtime(9, 15)) & (t < dtime(15, 30))].reset_index(drop=True)


def _fut_candles(sid: str, day: date) -> pd.DataFrame:
    import dhan_client
    body = {"securityId": str(sid), "exchangeSegment": "NSE_FNO", "instrument": "FUTIDX", "interval": "1", "oi": True,
            "fromDate": f"{day:%Y-%m-%d} 09:15:00", "toDate": f"{day:%Y-%m-%d} 15:30:00"}
    return _payload_to_df(dhan_client._post("/charts/intraday", body))


def _bank_contract() -> Optional[dict]:
    """Nearest unexpired BANKNIFTY FUTIDX, price-checked against the BANKNIFTY index."""
    import dhan_client
    df = pd.read_csv(dhan_client.SCRIP_MASTER, low_memory=False)
    cols = {c.upper().replace(" ", "_"): c for c in df.columns}
    g = lambda *n: next((cols[x] for x in n if x in cols), None)          # noqa: E731
    seg, inst, sym = g("SEM_EXM_EXCH_ID"), g("SEM_INSTRUMENT_NAME"), g("SEM_TRADING_SYMBOL")
    exp, sid = g("SEM_EXPIRY_DATE"), g("SEM_SMST_SECURITY_ID")
    m = df[(df[seg].astype(str).str.upper() == "NSE") & (df[inst].astype(str).str.upper() == "FUTIDX")
           & (df[sym].astype(str).str.upper().str.startswith("BANKNIFTY-"))].copy()
    m["_e"] = pd.to_datetime(m[exp], errors="coerce")
    m = m[m["_e"].dt.date >= date.today()].sort_values("_e")
    try:
        idx = float(dhan_client.banknifty_index_candles(3)["close"].iloc[-1])
    except Exception:                                        # noqa: BLE001
        idx = None
    for _, row in m.head(3).iterrows():
        s = str(int(row[sid]))
        if idx:
            try:
                last = _fut_candles(s, date.today())["c"].dropna()
                if len(last) and abs(last.iat[-1] - idx) / idx * 100 > R["max_basis_pct"]:
                    continue
            except Exception:                                # noqa: BLE001
                pass
        return {"id": s, "symbol": str(row[sym]), "expiry": row["_e"].date()}
    return None


# ── Streamlit ────────────────────────────────────────────────────────────────
def _verdict_card(st, name: str, s: Optional[dict]):
    if not s:
        st.markdown(f"<div style='background:{PANEL};padding:10px;border-radius:8px;color:{MUTED}'>{name}: no OI data</div>",
                    unsafe_allow_html=True)
        return
    c = BUILD_CLR.get(s["dominant"], GOLD)
    sg = f"<br><span style='color:{ORANGE}'>⚡ OI surge {s['surge']['ts']:%H:%M} ({s['surge']['x']}×) — big move building</span>" \
        if s.get("surge") else ""
    st.markdown(
        f"<div style='background:{PANEL};border-left:4px solid {c};padding:10px 14px;border-radius:8px'>"
        f"<span style='color:{GOLD};font-weight:700'>{name}</span><span style='color:#cbd5e1'> · last {s['bars']} candles: </span>"
        f"<b style='color:{c}'>{s['dominant'].upper()}</b><br>"
        f"<span style='color:{WHITE}'>net ΔOI {s['net_doi']:+,} · OI {s['oi']:,} · last candle: {s['last_build']}</span>{sg}</div>",
        unsafe_allow_html=True)


def render(day: date, tf: str = "5m") -> None:
    import streamlit as st
    import dhan_client

    st.markdown(
        f"""<div style="background:{NAVY};border-bottom:3px solid {GOLD};padding:12px 16px;border-radius:8px;margin-bottom:10px">
        <span style="color:{GOLD};font-size:1.2rem;font-weight:800">📈 Futures OI · EMA 8</span>
        <span style="color:{WHITE};margin-left:10px">NIFTY + BANKNIFTY current-month futures — build-up, OI surge, confirmation</span>
        </div>""", unsafe_allow_html=True)
    minutes = int(str(tf).rstrip("m") or 5)

    @st.cache_data(ttl=6 * 3600, show_spinner=False)
    def _contracts(_d: date):
        n = dhan_client.nifty_futures_contract()
        try:
            b = _bank_contract()
        except Exception as e:                               # noqa: BLE001
            b = {"error": str(e)[:160]}
        return n, b

    @st.cache_data(ttl=60, show_spinner=False)
    def _load(sid: str, d: date) -> pd.DataFrame:
        return _fut_candles(sid, d)

    with st.spinner("Resolving futures contracts…"):
        nc, bc = _contracts(date.today())
    res = {}
    for name, c in (("NIFTY", nc), ("BANKNIFTY", bc)):
        if not c or "id" not in c:
            st.warning(f"{name} future not resolved: {(c or {}).get('error', 'scrip master lookup failed')}")
            continue
        try:
            raw = _load(c["id"], day)
        except Exception as e:                               # noqa: BLE001
            st.error(f"{name} future candles failed: {e}")
            continue
        if raw.empty or raw["oi"].isna().all():
            st.info(f"{name}: Dhan returned no OI for {day:%d %b} (contract {c.get('symbol')}).")
            continue
        res[name] = (c, analyse(resample(raw, minutes)))

    if not res:
        return
    sums = {k: summary(v[1]) for k, v in res.items()}
    cmp = compare(sums.get("NIFTY"), sums.get("BANKNIFTY"))
    a, b = st.columns(2)
    with a:
        _verdict_card(st, "NIFTY FUT", sums.get("NIFTY"))
    with b:
        _verdict_card(st, "BANKNIFTY FUT", sums.get("BANKNIFTY"))
    if cmp:
        cc = {"ALIGNED": BLUE, "DIVERGING": ORANGE}.get(cmp["verdict"], MUTED)
        st.markdown(f"<div style='background:{NAVY};border:1px solid {cc};padding:8px 14px;border-radius:8px;margin:8px 0'>"
                    f"<b style='color:{cc}'>NIFTY vs BANKNIFTY: {cmp['verdict']}</b> "
                    f"<span style='color:{WHITE}'>— {cmp['text']}</span></div>", unsafe_allow_html=True)

    for name, (c, bars) in res.items():
        st.plotly_chart(figure(bars, name, tf), use_container_width=True, key=f"futoi_{name}_{tf}")
        st.caption(f"{c.get('symbol')} (id {c['id']}) · {len(bars)} candles · "
                   f"{bars['ts'].iat[0]:%H:%M}–{bars['ts'].iat[-1]:%H:%M}")

    with st.expander("ℹ️ WHAT IT IS · HOW TO READ", expanded=False):
        st.markdown(
            "- **Build-up** (each bar of ΔOI is coloured): 🟩 long build-up (price ↑ OI ↑) · 🟢 short covering (price ↑ OI ↓) · "
            "🟥 short build-up (price ↓ OI ↑) · 🩷 long unwinding (price ↓ OI ↓) · grey = too small to count.\n"
            "- **OI vs its 8-EMA**: OI above a rising EMA = positions are being ADDED; below = positions being closed.\n"
            "- **⚡ OI surge**: ΔOI ≥ 2.5× normal and OI above its EMA — a big move is building. It does NOT say which way: "
            "read the build-up colour and your structure (BOS / MSS, CRT).\n"
            "- **NIFTY vs BANKNIFTY**: same side = broad, trustworthy move. Opposite sides = narrow move that often fails — "
            "that is exactly where traps work.\n"
            "- Short covering rallies fade once covering is done; long build-up rallies can run. Same price move, different fuel.\n"
            "- Information only — not a signal and not advice.")
