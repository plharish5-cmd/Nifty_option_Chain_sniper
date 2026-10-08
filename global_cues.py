"""
global_cues.py — "🌏 Pre-market & Gap" sub-tab of 🧪 Options Lab + the 09:05 Telegram brief.

  Cues (free, Yahoo Finance via yfinance — runs on your laptop):
    US close        S&P 500 · Nasdaq · Dow · US VIX · India ADRs (Infosys, HDFC Bank, ICICI Bank, Wipro, Dr Reddy's)
    US futures      S&P / Nasdaq / Dow futures — the move SINCE the US close (overnight news)
    Asia (live)     Nikkei · KOSPI · Hang Seng · Shanghai · Taiwan · ASX — today's open vs their previous close
    Europe close    DAX · FTSE · CAC · Euro Stoxx 50
    Macro           Brent & WTI crude · USD/INR · Dollar index · US 10-year yield · Gold
    India           GIFT NIFTY (you type it — no free feed; 1 number from any app at 8:45–9:05) · India VIX
  Model   YOUR history: NIFTY open-gap % (data/ohlc) regressed on the same cues for the last ~2 years →
          predicted gap, which cues actually mattered (R², weights), and agreement with GIFT NIFTY.
  Gap stats  from YOUR daily OHLC: how often gaps of that size and direction FILLED the same day, closed in the gap's
          direction, and the typical range — plus your course rule: gap UP blocks CE, gap DOWN blocks PE.
Information only — not financial advice. A gap model explains part of the gap, never all of it.
"""
from __future__ import annotations

import json
import time as _time
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

GROUPS = {
    "US close": {"^GSPC": "S&P 500", "^IXIC": "Nasdaq", "^DJI": "Dow Jones", "^VIX": "US VIX"},
    "India ADRs": {"INFY": "Infosys ADR", "HDB": "HDFC Bank ADR", "IBN": "ICICI Bank ADR", "WIT": "Wipro ADR",
                   "RDY": "Dr Reddy's ADR"},
    "US futures": {"ES=F": "S&P fut", "NQ=F": "Nasdaq fut", "YM=F": "Dow fut"},
    "Asia": {"^N225": "Nikkei", "^KS11": "KOSPI", "^HSI": "Hang Seng", "000001.SS": "Shanghai", "^TWII": "Taiwan",
             "^AXJO": "ASX 200"},
    "Europe": {"^GDAXI": "DAX", "^FTSE": "FTSE 100", "^FCHI": "CAC 40", "^STOXX50E": "Euro Stoxx 50"},
    "Macro": {"BZ=F": "Brent crude", "CL=F": "WTI crude", "INR=X": "USD/INR", "DX-Y.NYB": "Dollar index",
              "^TNX": "US 10Y yield", "GC=F": "Gold"},
    "India": {"^INDIAVIX": "India VIX", "^NSEI": "NIFTY 50"},
}
ALL = {k: v for g in GROUPS.values() for k, v in g.items()}
# sign a NIFTY trader expects (+1 = up is good for NIFTY)
SIGN = {"^VIX": -1, "BZ=F": -1, "CL=F": -1, "INR=X": -1, "DX-Y.NYB": -1, "^TNX": -1, "^INDIAVIX": -1, "GC=F": 0}
ASIA_OPEN = {"^N225", "^KS11", "^HSI", "000001.SS", "^TWII", "^AXJO"}       # use TODAY's open-gap (known before 09:15)


def _dir() -> Path:
    import config
    d = Path(config.DATA_DIR) / "global"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ═══════════════════════════════════════════════════════════════════════════
# data
# ═══════════════════════════════════════════════════════════════════════════
def history(force: bool = False) -> dict:
    """{ticker: DataFrame(Open, Close)} daily, ~3 years, cached once a day."""
    f = _dir() / "daily.pkl"
    if f.exists() and not force and date.fromtimestamp(f.stat().st_mtime) == date.today():
        try:
            return pd.read_pickle(f)
        except Exception:                                     # noqa: BLE001
            pass
    import yfinance as yf
    raw = yf.download(list(ALL), period="3y", interval="1d", group_by="ticker", auto_adjust=False,
                      progress=False, threads=True)
    out = {}
    for t in ALL:
        try:
            d = raw[t][["Open", "Close"]].dropna(how="all")
            d.index = pd.to_datetime(d.index).tz_localize(None).normalize()
            if len(d):
                out[t] = d
        except Exception:                                     # noqa: BLE001
            continue
    pd.to_pickle(out, f)
    return out


def live(max_age: float = 120) -> pd.DataFrame:
    """Latest value vs previous close for every cue (5-day daily bars: the last bar is live for open markets)."""
    f = _dir() / "live.pkl"
    if f.exists() and _time.time() - f.stat().st_mtime < max_age:
        try:
            return pd.read_pickle(f)
        except Exception:                                     # noqa: BLE001
            pass
    import yfinance as yf
    raw = yf.download(list(ALL), period="7d", interval="1d", group_by="ticker", auto_adjust=False,
                      progress=False, threads=True)
    rows = []
    for g, items in GROUPS.items():
        for t, nm in items.items():
            try:
                d = raw[t][["Open", "Close"]].dropna()
                d.index = pd.to_datetime(d.index).tz_localize(None)
                last, prev = d.iloc[-1], d.iloc[-2]
                chg = 100 * (last.Close / prev.Close - 1)
                gap = 100 * (last.Open / prev.Close - 1)
                rows.append(dict(group=g, ticker=t, name=nm, last=float(last.Close), prev=float(prev.Close),
                                 chg=float(chg), gap=float(gap), asof=d.index[-1]))
            except Exception:                                 # noqa: BLE001
                rows.append(dict(group=g, ticker=t, name=nm, last=np.nan, prev=np.nan, chg=np.nan, gap=np.nan, asof=pd.NaT))
    x = pd.DataFrame(rows)
    x.to_pickle(f)
    return x


def gift() -> dict:
    try:
        return json.loads((_dir() / "gift_nifty.json").read_text())
    except Exception:                                         # noqa: BLE001
        return {}


def save_gift(v: float, fut_prev: float | None) -> None:
    (_dir() / "gift_nifty.json").write_text(json.dumps(dict(value=float(v), fut_prev=fut_prev,
                                                            at=datetime.now().isoformat(timespec="minutes"))))


def nifty_daily() -> pd.DataFrame:
    import levels
    d = levels.load_ohlc().copy()
    d["Date"] = pd.to_datetime(d["Date"]).dt.normalize()
    d = d.sort_values("Date").drop_duplicates("Date").set_index("Date")
    for c in ("Open", "High", "Low", "Close"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d["prev"] = d.Close.shift()
    d["gap"] = 100 * (d.Open / d.prev - 1)
    d["gap_pts"] = d.Open - d.prev
    return d.dropna(subset=["prev"])


# ═══════════════════════════════════════════════════════════════════════════
# model
# ═══════════════════════════════════════════════════════════════════════════
FEATS = ["^GSPC", "^IXIC", "ADR", "^VIX", "BZ=F", "INR=X", "DX-Y.NYB", "^TNX", "^N225", "^HSI", "^KS11", "^GDAXI"]


def features(h: dict, days: pd.DatetimeIndex) -> pd.DataFrame:
    """Row per NIFTY date: cue returns KNOWN before 09:15 that day (US / Europe / macro: last session before;
    Asia: that morning's open-gap)."""
    cols = {}
    for t in FEATS:
        if t == "ADR":
            parts = [100 * h[a].Close.pct_change() for a in ("INFY", "HDB", "IBN") if a in h]
            if not parts:
                continue
            s = pd.concat(parts, axis=1).mean(axis=1)
            ref_prev = True
        elif t not in h:
            continue
        elif t in ASIA_OPEN:
            d = h[t]
            s = 100 * (d.Open / d.Close.shift() - 1)
            ref_prev = False
        else:
            s = 100 * h[t].Close.pct_change()
            ref_prev = True
        s = s.dropna().sort_index()
        if ref_prev:                                        # last value strictly BEFORE the NIFTY date
            idx = s.index.searchsorted(days, side="left") - 1
            vals = np.where(idx >= 0, s.values[np.clip(idx, 0, None)], np.nan)
        else:                                               # same calendar date
            vals = s.reindex(days).values
        cols[t] = vals
    return pd.DataFrame(cols, index=days)


def fit(h: dict, nd: pd.DataFrame, years: float = 2.0) -> dict | None:
    nd = nd[nd.index >= nd.index.max() - pd.Timedelta(days=int(365 * years))]
    X = features(h, nd.index)
    y = nd.gap
    ok = X.notna().all(axis=1) & y.notna() & (y.abs() < 5)
    X, y = X[ok], y[ok]
    if len(y) < 60:
        return None
    mu, sd = X.mean(), X.std().replace(0, 1)
    Z = (X - mu) / sd
    A = np.c_[np.ones(len(Z)), Z.values]
    lam = 1.0                                               # light ridge: cues are correlated (S&P vs Nasdaq)
    beta = np.linalg.solve(A.T @ A + lam * np.eye(A.shape[1]) * np.r_[0, np.ones(Z.shape[1])], A.T @ y.values)
    pred = A @ beta
    r2 = 1 - ((y.values - pred) ** 2).sum() / ((y.values - y.mean()) ** 2).sum()
    corr = {c: float(np.corrcoef(X[c], y)[0, 1]) for c in X.columns}
    sign_hit = float(((np.sign(pred) == np.sign(y.values)) | (y.abs().values < 0.05)).mean())
    return dict(beta=beta, mu=mu, sd=sd, cols=list(X.columns), r2=float(r2), n=int(len(y)), corr=corr,
                resid_sd=float(np.std(y.values - pred)), sign_hit=sign_hit)


def today_x(lv: pd.DataFrame, cols: list[str]) -> dict:
    v = lv.set_index("ticker")
    x = {}
    for c in cols:
        if c == "ADR":
            a = [v.at[t, "chg"] for t in ("INFY", "HDB", "IBN") if t in v.index and np.isfinite(v.at[t, "chg"])]
            x[c] = float(np.mean(a)) if a else np.nan
        elif c in v.index:
            x[c] = float(v.at[c, "gap"] if c in ASIA_OPEN else v.at[c, "chg"])
        else:
            x[c] = np.nan
    return x


def predict(m: dict, x: dict) -> tuple[float, dict]:
    z, contrib = [], {}
    for i, c in enumerate(m["cols"]):
        v = x.get(c, np.nan)
        zi = 0.0 if not np.isfinite(v) else (v - m["mu"][c]) / m["sd"][c]
        z.append(zi)
        contrib[c] = float(m["beta"][i + 1] * zi)
    return float(m["beta"][0] + sum(contrib.values())), contrib


def gap_stats(nd: pd.DataFrame, gap_pct: float, band: float = 0.25) -> dict:
    """Your history: days that opened with a gap of the same sign within ±band % of this one."""
    s = np.sign(gap_pct) or 1
    sel = nd[(np.sign(nd.gap) == s) & ((nd.gap - gap_pct).abs() <= band)]
    if len(sel) < 5:
        sel = nd[(np.sign(nd.gap) == s) & (nd.gap.abs() >= max(0.1, abs(gap_pct) / 2))]
    if sel.empty:
        return dict(n=0)
    filled = (sel.Low <= sel.prev) if s > 0 else (sel.High >= sel.prev)
    cont = (sel.Close > sel.Open) if s > 0 else (sel.Close < sel.Open)
    closed_dir = (sel.Close > sel.prev) if s > 0 else (sel.Close < sel.prev)
    return dict(n=int(len(sel)), fill=100 * float(filled.mean()), cont=100 * float(cont.mean()),
                closed_dir=100 * float(closed_dir.mean()), rng=float((sel.High - sel.Low).mean()),
                last=sel.tail(5))


# ═══════════════════════════════════════════════════════════════════════════
# the read (shared by the tab and the Telegram brief)
# ═══════════════════════════════════════════════════════════════════════════
def assess(gap_rule_pts: float = 50.0) -> dict:
    h, lv, nd = history(), live(), nifty_daily()
    m = fit(h, nd)
    out = dict(live=lv, model=m, nd=nd)
    prev = float(nd.Close.iloc[-1]) if nd.index[-1].date() < date.today() else float(nd.prev.iloc[-1])
    out["prev_close"] = prev
    if m:
        x = today_x(lv, m["cols"])
        p, contrib = predict(m, x)
        out.update(model_gap=p, model_pts=prev * p / 100, contrib=contrib, x=x)
    g = gift()
    if g.get("value") and g.get("at", "")[:10] == date.today().isoformat():
        base = float(g.get("fut_prev") or 0) or prev
        out["gift_gap"] = 100 * (g["value"] / base - 1)
        out["gift_pts"] = prev * out["gift_gap"] / 100
        out["gift_base"] = "NIFTY futures prev close" if g.get("fut_prev") else "NIFTY spot prev close (includes futures premium)"
    best = out.get("gift_gap", out.get("model_gap"))
    out["best_gap"] = best
    if best is not None and np.isfinite(best):
        out["best_pts"] = prev * best / 100
        out["stats"] = gap_stats(nd, best)
        pts = out["best_pts"]
        out["rule"] = ("GAP UP → your rule blocks CE trades today" if pts >= gap_rule_pts else
                       "GAP DOWN → your rule blocks PE trades today" if pts <= -gap_rule_pts else
                       f"flat open (|gap| < {gap_rule_pts:.0f} pts) — no gap block")
    return out


def score_cues(lv: pd.DataFrame) -> tuple[int, int, list]:
    up = dn = 0
    lines = []
    for r in lv.itertuples():
        if not np.isfinite(r.chg) or r.ticker in ("^NSEI", "GC=F"):
            continue
        v = r.gap if r.ticker in ASIA_OPEN else r.chg
        s = SIGN.get(r.ticker, 1) * np.sign(v)
        if abs(v) < 0.15:
            continue
        up += s > 0
        dn += s < 0
    return int(up), int(dn), lines


def brief_text(a: dict | None = None) -> str:
    a = a or assess()
    lv = a["live"].set_index("ticker")
    f = lambda t: (f"{lv.at[t, 'name']} {lv.at[t, 'gap' if t in ASIA_OPEN else 'chg']:+.2f}%"
                   if t in lv.index and np.isfinite(lv.at[t, "chg"]) else None)
    up, dn, _ = score_cues(a["live"])
    L = ["<b>🌏 NIFTY PRE-MARKET</b>"]
    if "gift_gap" in a:
        L.append(f"GIFT NIFTY → gap <b>{a['gift_pts']:+,.0f} pts ({a['gift_gap']:+.2f}%)</b>")
    if "model_gap" in a:
        m = a["model"]
        L.append(f"Global-cue model → <b>{a['model_pts']:+,.0f} pts ({a['model_gap']:+.2f}%)</b> "
                 f"(R² {m['r2']:.2f}, ±{a['prev_close'] * m['resid_sd'] / 100:,.0f} pts typical miss)")
    L.append(f"Cues: {up} positive / {dn} negative for NIFTY")
    for grp in (["^GSPC", "^IXIC", "ES=F", "NQ=F"], ["INFY", "HDB", "IBN"], ["^N225", "^HSI", "^KS11"],
                ["BZ=F", "INR=X", "DX-Y.NYB", "^TNX", "^VIX"]):
        s = [x for x in (f(t) for t in grp) if x]
        if s:
            L.append(" · ".join(s))
    st_ = a.get("stats") or {}
    if st_.get("n"):
        L.append(f"Your history ({st_['n']} similar gaps): filled same day {st_['fill']:.0f}% · "
                 f"closed in gap direction {st_['closed_dir']:.0f}% · avg range {st_['rng']:,.0f} pts")
    if a.get("rule"):
        L.append(f"⚠ {a['rule']}")
    L.append("No trade on the first 15-min candle. Info only — not advice.")
    return "\n".join(L)


# ═══════════════════════════════════════════════════════════════════════════
# Streamlit tab
# ═══════════════════════════════════════════════════════════════════════════
def render(day: date | None = None) -> None:
    import streamlit as st
    import plotly.graph_objects as go
    from options_lab_tab import NAVY, GOLD, WHITE, MUTED, GREEN, RED

    def card(title, body, border=GOLD):
        return (f"<div style='background:{NAVY};border:1px solid {border};border-radius:8px;padding:10px 14px;margin:6px 0'>"
                f"<div style='color:{GOLD};font-weight:700'>{title}</div>"
                f"<div style='color:{WHITE};margin-top:4px;line-height:1.6'>{body}</div></div>")

    try:
        import yfinance  # noqa: F401
    except ImportError:
        st.error("yfinance is not installed — run once:  .venv/bin/pip install yfinance   then refresh.")
        return
    g = gift()
    c = st.columns([1.4, 1.4, 1.2, 1.2])
    gv = c[0].number_input("GIFT NIFTY now", 0.0, 100000.0, float(g.get("value") or 0.0), 0.5, key="gc_gift",
                           help="Type the live GIFT NIFTY price (NSE IX / any broker app) between 8:45 and 9:10")
    fp = c[1].number_input("NIFTY fut prev close (optional)", 0.0, 100000.0, float(g.get("fut_prev") or 0.0), 0.5,
                           key="gc_futprev", help="GIFT NIFTY trades like the NIFTY future; comparing it with the future's "
                                                  "close removes the futures premium")
    rule = c[2].number_input("Gap rule (pts)", 5.0, 300.0, 50.0, 5.0, key="gc_rule",
                             help="Your rule: gap up blocks CE, gap down blocks PE — from this size")
    if c[3].button("💾 Save GIFT & refresh", key="gc_save", use_container_width=True):
        if gv > 0:
            save_gift(gv, fp or None)
        try:
            (_dir() / "live.pkl").unlink()
        except Exception:                                     # noqa: BLE001
            pass
    with st.spinner("Reading global markets (Yahoo Finance)…"):
        try:
            a = assess(rule)
        except Exception as exc:                              # noqa: BLE001
            st.error(f"Could not read global cues: {exc}")
            return
    lv = a["live"]
    pc = a["prev_close"]
    up, dn, _ = score_cues(lv)
    k = st.columns(4)
    if "gift_gap" in a:
        k[0].metric("GIFT NIFTY gap", f"{a['gift_pts']:+,.0f} pts", f"{a['gift_gap']:+.2f}%")
    else:
        k[0].metric("GIFT NIFTY gap", "type GIFT ↑")
    if "model_gap" in a:
        k[1].metric("Global-cue model", f"{a['model_pts']:+,.0f} pts", f"{a['model_gap']:+.2f}% · R² {a['model']['r2']:.2f}",
                    delta_color="off")
    k[2].metric("Cues for NIFTY", f"{up} ▲ / {dn} ▼")
    k[3].metric("NIFTY prev close", f"{pc:,.1f}")

    # cue board
    cells = []
    for grp, items in GROUPS.items():
        sub = lv[lv.group == grp]
        row = []
        for r in sub.itertuples():
            v = r.gap if r.ticker in ASIA_OPEN else r.chg
            if not np.isfinite(v):
                row.append(f"<span style='color:{MUTED}'>{r.name} —</span>")
                continue
            good = SIGN.get(r.ticker, 1) * v
            col = GREEN if good > 0.05 else RED if good < -0.05 else WHITE
            row.append(f"<span style='white-space:nowrap'>{r.name} <b style='color:{col}'>{v:+.2f}%</b>"
                       f"<span style='color:{MUTED};font-size:0.75rem'> {r.last:,.2f}</span></span>")
        note = {"Asia": " (today's open vs prev close)", "US futures": " (since US close)",
                "Macro": " (red = bad for NIFTY: crude ↑, rupee weak ↑, dollar ↑, yields ↑)"}.get(grp, "")
        cells.append(f"<div style='margin:4px 0'><b style='color:{GOLD}'>{grp}</b><span style='color:{MUTED};font-size:0.75rem'>"
                     f"{note}</span><br>{' · '.join(row)}</div>")
    st.markdown(card("GLOBAL CUES", "".join(cells)), unsafe_allow_html=True)

    m = a.get("model")
    if m:
        lab = {"ADR": "India ADRs"} | {t: ALL.get(t, t) for t in m["cols"]}
        ct = pd.Series(a["contrib"]).rename(index=lab).sort_values()
        fig = go.Figure(go.Bar(x=ct.values * pc / 100, y=ct.index, orientation="h",
                               marker_color=np.where(ct.values >= 0, GREEN, RED),
                               hovertemplate="%{y}: %{x:+.0f} NIFTY pts<extra></extra>"))
        fig.update_layout(title=dict(text="<b>What the model says each cue adds to the gap (NIFTY pts)</b>",
                                     font=dict(color=GOLD, size=14)), height=360, paper_bgcolor=NAVY, plot_bgcolor="#10223f",
                          font=dict(color=WHITE), margin=dict(l=10, r=10, t=45, b=10))
        fig.update_xaxes(zeroline=True, zerolinecolor=WHITE, tickformat="+.0f", gridcolor="#1e3358")
        st.plotly_chart(fig, use_container_width=True, key="gc_contrib")
        top = sorted(m["corr"].items(), key=lambda kv: -abs(kv[1]))[:5]
        st.caption(f"Model on YOUR last {m['n']} sessions: R² {m['r2']:.2f} · direction right {100 * m['sign_hit']:.0f}% · "
                   f"typical miss ±{pc * m['resid_sd'] / 100:,.0f} pts. Strongest cues: "
                   + ", ".join(f"{lab.get(k_, k_)} ({v:+.2f})" for k_, v in top))

    L = []
    if "gift_gap" in a and "model_gap" in a:
        agree = np.sign(a["gift_gap"]) == np.sign(a["model_gap"]) or abs(a["model_pts"]) < 20
        L.append(f"GIFT NIFTY says <b>{a['gift_pts']:+,.0f}</b>, the global-cue model says <b>{a['model_pts']:+,.0f}</b> — "
                 + (("the model sees a flat global night — the gap is India-specific (news / flows)." if abs(a["model_pts"]) < 20
                     else "they AGREE on direction.") if agree else
                    "they DISAGREE: India-specific news / flows are driving GIFT — trust GIFT, expect a volatile open."))
        L.append(f"GIFT gap measured against the {a['gift_base']}.")
    elif "model_gap" in a:
        L.append(f"Model gap <b>{a['model_pts']:+,.0f} pts</b>. Type GIFT NIFTY above for the real pre-open read (it already "
                 "contains everything the model sees plus India news).")
    st_ = a.get("stats") or {}
    if st_.get("n"):
        L.append(f"<b>Your history</b>, {st_['n']} sessions that opened with a similar gap: filled the gap the same day "
                 f"<b>{st_['fill']:.0f}%</b> · kept going after the open {st_['cont']:.0f}% · closed beyond the previous "
                 f"close {st_['closed_dir']:.0f}% · average day range {st_['rng']:,.0f} pts.")
        if st_["fill"] >= 60:
            L.append("High fill rate → expect a move back toward the previous close in the first hour; don't chase the gap.")
        elif st_["fill"] <= 35:
            L.append("Low fill rate → gaps this size usually HOLD: the previous close is unlikely to be revisited early.")
    if a.get("rule"):
        L.append(f"<b style='color:{GOLD}'>⚠ {a['rule']}</b> · never trade the first 15-min candle.")
    if L:
        st.markdown(card("★ PRE-MARKET READ", "<br>".join(L)), unsafe_allow_html=True)
    with st.expander("Other things that move the open (check them yourself)"):
        st.markdown("- **FII / DII cash** of the previous day (your FII/DII tab) — big FII selling days often gap lower next day.\n"
                    "- **Events tonight / today** (📅 Events tab): RBI, Fed, CPI, results of HDFC Bank / Reliance / ICICI.\n"
                    "- **Previous day's close location**: a close at the day's low tends to open weak; at the high, strong.\n"
                    "- **Weekend / holiday gaps** are bigger: two-three days of global news in one open.\n"
                    "- **Crude above ~$90 and a weak rupee** are a double negative for NIFTY (oil importer).")
    st.caption("Data: Yahoo Finance (delayed up to 15 min for some markets). Information only — not financial advice.")
