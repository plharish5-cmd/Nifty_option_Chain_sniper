"""
max_pain_pro.py — "🎯 Max Pain" sub-tab of 🧪 Options Lab (StockMojo's Max Pain page, same charts & settings,
plus the reads most traders never look at).

  Chart 1 · Max Pain vs Future   max-pain strike through the day (step line) with the NIFTY future, the previous
            session alongside (like StockMojo), every max-pain SHIFT marked, and the "magnet zone" shaded.
  Chart 2 · Max Pain             call pain (green) / put pain (red) per strike = what option BUYERS would be paid if
            NIFTY expired at that strike (₹ B, same scale as StockMojo) + total pain (gold) + spot / max-pain lines.
            "View at" replays the curve at any time of the session.
  Chart 3 · Distance to magnet   spot − max pain through the day against ± ½ of the ATM straddle (reach band).
  Cards    Market Sentiment (StockMojo-style) and the PRO read: drift, writers following / not following price,
           magnet strength by days-to-expiry, pain asymmetry (which way a move gets fuel), magnet zone, walls,
           narrative for the DAY and for every HOUR, what to do / what not to do.

Max pain uses ALL strikes of the expiry (the correct definition); "Strikes above and below ATM" only changes what
the bar chart shows. Information only — not financial advice. Max-pain reads are not backtested in this app.
"""
from __future__ import annotations

from datetime import date, time
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go

import options_lab_tab as ol
from options_lab_tab import NAVY, GOLD, WHITE, MUTED, GREEN, RED, FUT

ORANGE = "#f97316"
PANEL = "#10223f"
TF = {"1m": 1, "3m": 3, "5m": 5, "15m": 15}
BREAKS = [dict(bounds=["sat", "mon"]), dict(bounds=[15.55, 9.2], pattern="hour")]


# ═══════════════════════════════════════════════════════════════════════════
# 1 · Data
# ═══════════════════════════════════════════════════════════════════════════
def _exp_date(e) -> pd.Timestamp:
    s = str(e)
    return pd.to_datetime(s, errors="coerce", dayfirst=not s[:4].isdigit())


def expiries(snaps: list) -> list[str]:
    seen = {}
    for _, _, df in snaps[-3:] + snaps[:3]:
        if df is not None and "expiry" in df:
            for e in df["expiry"].dropna().unique():
                seen[str(e)] = _exp_date(e)
    return [k for k, v in sorted(seen.items(), key=lambda kv: (pd.isna(kv[1]), kv[1]))]


def frames(snaps: list, expiry: str | None) -> list[tuple]:
    """[(ts, spot, strikes, ce_oi, pe_oi, ce_ltp, pe_ltp)] for one expiry, strikes sorted."""
    out = []
    for ts, spot, df in snaps:
        if df is None or df.empty:
            continue
        d = df
        if expiry is not None and "expiry" in d:
            d = d[d["expiry"].astype(str) == str(expiry)]
            if d.empty:
                continue
        elif "expiry" in d:
            d = ol._one_expiry(d)
        d = d.drop_duplicates("strike")
        k = pd.to_numeric(d["strike"], errors="coerce").to_numpy(float)
        ok = np.isfinite(k)
        if ok.sum() < 5:
            continue
        o = np.argsort(k[ok])
        g = lambda c: pd.to_numeric(d[c], errors="coerce").fillna(0).to_numpy(float)[ok][o] if c in d else np.zeros(ok.sum())
        out.append((pd.Timestamp(ts), float(spot), k[ok][o], g("ce_oi"), g("pe_oi"), g("ce_ltp"), g("pe_ltp")))
    return out


def share_mult(fr: list) -> tuple[float, str]:
    """OI → shares. Uses your Options Lab oi_multiplier; if OI is clearly in LOTS, × lot size (StockMojo uses shares)."""
    r = ol._rules()
    m = float(r.get("oi_multiplier", 1) or 1)
    try:
        import config
        lot = float(getattr(config, "LOT_SIZE", 0) or getattr(config, "NIFTY_LOT_SIZE", 0) or 65)
    except Exception:                                           # noqa: BLE001
        lot = 65.0
    tot = float(fr[-1][3].sum() + fr[-1][4].sum()) * m if fr else 0
    if m == 1 and 0 < tot < 5e6:
        return lot, f"OI looks like lots → ×{lot:.0f} to shares"
    return m, "OI as shares" if m == 1 else f"OI ×{m:g}"


def pain(ks: np.ndarray, ce: np.ndarray, pe: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """call_pain[i], put_pain[i] = payout to call / put buyers if NIFTY settles at ks[i] (OI units × points)."""
    diff = ks[:, None] - ks[None, :]                            # settle − strike
    return (np.maximum(diff, 0) * ce[None, :]).sum(1), (np.maximum(-diff, 0) * pe[None, :]).sum(1)


def _pain_at(x: float, ks, ce, pe) -> float:
    return float((np.maximum(x - ks, 0) * ce).sum() + (np.maximum(ks - x, 0) * pe).sum())


def snapshot_stats(f: tuple, step: float) -> dict:
    ts, spot, ks, ce, pe, cl, pl = f
    cp, pp = pain(ks, ce, pe)
    tot = cp + pp
    i = int(np.argmin(tot))
    mp, mn = float(ks[i]), float(tot[i])
    zone = ks[tot <= mn * 1.10] if mn > 0 else ks[[i]]
    lo, hi = float(zone.min()), float(zone.max())
    nb = [tot[j] for j in (i - 2, i + 2) if 0 <= j < len(tot)]
    sharp = (float(np.mean(nb)) - mn) / mn if mn > 0 and nb else np.nan
    a = int(np.argmin(np.abs(ks - spot)))
    strad = float(cl[a] + pl[a]) if cl[a] > 0 and pl[a] > 0 else np.nan
    p0 = _pain_at(spot, ks, ce, pe)
    oi = ce + pe
    cog = float((ks * oi).sum() / oi.sum()) if oi.sum() else np.nan
    return dict(ts=ts, spot=spot, mp=mp, pain_min=mn, pain_spot=p0, zlo=lo, zhi=hi, sharp=sharp, straddle=strad,
                up100=_pain_at(spot + 2 * step, ks, ce, pe) - p0, dn100=_pain_at(spot - 2 * step, ks, ce, pe) - p0,
                cog=cog, ce_wall=float(ks[int(np.argmax(ce))]), pe_wall=float(ks[int(np.argmax(pe))]))


def series(fr: list, step: float) -> pd.DataFrame:
    return pd.DataFrame([snapshot_stats(f, step) for f in fr])


def add_future(s: pd.DataFrame, fut: pd.DataFrame | None) -> pd.DataFrame:
    s = s.copy()
    s["ts"] = pd.to_datetime(s["ts"]).astype("datetime64[ns]")
    if fut is None or fut.empty:
        s["fut"] = s["spot"]
        return s
    f = fut[["ts", "fut"]].copy()
    f["ts"] = pd.to_datetime(f["ts"]).dt.tz_localize(None).astype("datetime64[ns]")   # Dhan µs vs snapshots ns
    s = pd.merge_asof(s.sort_values("ts"), f.sort_values("ts"), on="ts", direction="nearest",
                      tolerance=pd.Timedelta(minutes=3))
    s["fut"] = s["fut"].interpolate(limit_direction="both").fillna(s["spot"])
    return s


def resample(s: pd.DataFrame, tf: str) -> pd.DataFrame:
    m = TF[tf]
    if m == 1 or s.empty:
        return s
    out = []
    for _, g in s.groupby(s["ts"].dt.date):
        out.append(g.set_index("ts").resample(f"{m}min", origin="start_day", offset="15min").last().dropna(subset=["mp"]))
    return pd.concat(out).reset_index()


def shifts(s: pd.DataFrame) -> pd.DataFrame:
    if s.empty:
        return s
    ch = s["mp"].diff().fillna(0) != 0
    x = s[ch].copy()
    x["from"] = s["mp"].shift(1)[ch]
    return x[["ts", "from", "mp", "spot", "fut"]]


def future_frame(snaps: list, day: date, want_real: bool) -> tuple[pd.DataFrame | None, str, str]:
    try:
        import pcr_oi_pro as po
        if want_real:
            f, why = po.future_1m(day)
            if f is not None and len(f) >= 3:
                return f, "NIFTY future", why
        else:
            why = "synth future chosen"
        return po.synth(snaps), "Synth future", why
    except Exception as exc:                                    # noqa: BLE001
        return None, "NIFTY spot", f"spot used ({str(exc)[:50]})"


# ═══════════════════════════════════════════════════════════════════════════
# 2 · Reads (the part most traders never look at)
# ═══════════════════════════════════════════════════════════════════════════
def trading_days_to_expiry(day: date, expiry: str) -> int | None:
    e = _exp_date(expiry)
    if pd.isna(e):
        return None
    return int(np.busday_count(day, e.date()))


def magnet(dte: int | None, now: pd.Timestamp, sharp: float, gap: float, strad: float) -> tuple[int, str, str]:
    """0–100 magnet strength: time to expiry × curve sharpness × reach (gap vs straddle). Heuristic, not backtested."""
    if dte is None:
        t = 0.3
    elif dte == 0:
        t = 0.55 + 0.45 * min(1.0, max(0.0, (now.hour * 60 + now.minute - 12 * 60) / 180))   # rises 12:00 → 15:00
    else:
        t = {1: 0.45, 2: 0.32}.get(dte, 0.18)
    sh = 0.6 if not np.isfinite(sharp) else float(np.clip(0.4 + sharp, 0.4, 1.0))
    reach = 1.0 if not (np.isfinite(strad) and strad > 0) else float(np.clip(1.25 - abs(gap) / strad, 0.15, 1.0))
    sc = int(round(100 * t * sh * reach))
    lab = "STRONG" if sc >= 55 else "MODERATE" if sc >= 30 else "WEAK"
    parts = ["expiry day" if dte == 0 else f"{dte} trading day(s) to expiry" if dte is not None else "expiry ?",
             f"curve {'sharp' if sh >= 0.8 else 'normal' if sh >= 0.55 else 'flat'}"]
    if np.isfinite(strad):
        parts.append(f"gap {abs(gap):,.0f} pts vs straddle {strad:,.0f}")
    why = " · ".join(parts)
    return sc, lab, why


def relation(d_px: float, d_mp: float, step: float) -> tuple[str, str]:
    """How max pain moved vs price over a window."""
    th_px, th_mp = 0.6 * step, step * 0.5
    if abs(d_px) < th_px and abs(d_mp) < th_mp:
        return "PIN", "price and max pain both quiet — a pinned / range hour"
    if d_px <= -th_px and d_mp <= -th_mp:
        return "BEAR-ACCEPT", "max pain FOLLOWED price down — writers re-positioned lower, the fall is being accepted"
    if d_px >= th_px and d_mp >= th_mp:
        return "BULL-ACCEPT", "max pain FOLLOWED price up — writers re-positioned higher, the rise is being accepted"
    if d_px <= -th_px:
        return "STRETCH-DN", "price fell but max pain did NOT follow — price stretched below the magnet (pull-back risk near expiry)"
    if d_px >= th_px:
        return "STRETCH-UP", "price rose but max pain did NOT follow — price stretched above the magnet (pull-back risk near expiry)"
    if d_mp > 0:
        return "MP-UP", "max pain moved UP while price stood still — writers making room above (mild bullish tilt)"
    return "MP-DN", "max pain moved DOWN while price stood still — writers making room below (mild bearish tilt)"


def window(s: pd.DataFrame, now: pd.Timestamp, minutes: int) -> pd.DataFrame:
    return s[(s.ts > now - pd.Timedelta(minutes=minutes)) & (s.ts <= now)]


def hourly(s_today: pd.DataFrame, step: float) -> pd.DataFrame:
    if s_today.empty:
        return pd.DataFrame()
    d0 = s_today.ts.iloc[0].normalize()
    edges = [d0 + pd.Timedelta(hours=9, minutes=15) + pd.Timedelta(hours=h) for h in range(7)] + [d0 + pd.Timedelta(hours=15, minutes=31)]
    rows = []
    for a, b in zip(edges[:-1], edges[1:]):
        g = s_today[(s_today.ts >= a) & (s_today.ts < b)]
        if len(g) < 2:
            continue
        f0, f1, m0, m1 = g.fut.iloc[0], g.fut.iloc[-1], g.mp.iloc[0], g.mp.iloc[-1]
        code, txt = relation(f1 - f0, m1 - m0, step)
        gap = g.spot.iloc[-1] - m1
        rows.append(dict(hour=f"{a:%H:%M}–{min(b, g.ts.iloc[-1] + pd.Timedelta(minutes=1)):%H:%M}",
                         fut0=f0, fut1=f1, d_fut=f1 - f0, mp0=m0, mp1=m1, d_mp=m1 - m0,
                         n_shift=int((g.mp.diff().fillna(0) != 0).sum()), gap=gap, code=code, read=txt))
    return pd.DataFrame(rows)


def _b(v_oi_pts: float, mult: float) -> float:
    return v_oi_pts * mult / 1e9                                 # ₹ billions (StockMojo scale)


def pro_read(s_today: pd.DataFrame, s_prev: pd.DataFrame, now_row: pd.Series, day: date, expiry: str,
             step: float, mult: float) -> dict:
    now = now_row.ts
    upto = s_today[s_today.ts <= now]
    mp, spot, fut = float(now_row.mp), float(now_row.spot), float(now_row.fut)
    gap, gap_f = spot - mp, fut - mp
    open_mp = float(upto.mp.iloc[0])
    prev_mp = float(s_prev.mp.iloc[-1]) if len(s_prev) else np.nan
    dte = trading_days_to_expiry(day, expiry)
    strad = float(now_row.straddle)
    sc, mag, mag_why = magnet(dte, now, float(now_row.sharp), gap, strad)
    w60 = window(upto, now, 60)
    rel60 = relation(float(w60.fut.iloc[-1] - w60.fut.iloc[0]), float(w60.mp.iloc[-1] - w60.mp.iloc[0]), step) \
        if len(w60) >= 2 else ("PIN", "not enough data yet")
    up, dn = _b(float(now_row.up100), mult), _b(float(now_row.dn100), mult)
    sh = shifts(upto)
    n_up, n_dn = int((sh.mp > sh["from"]).sum()), int((sh.mp < sh["from"]).sum())

    # sentiment (StockMojo uses future vs max pain; we use SPOT — the future carries cost-of-carry premium)
    if abs(gap) <= step * 0.5:
        sent, scol = "NEUTRAL · PINNED", GOLD
    elif gap < 0:
        sent, scol = "BEARISH", RED
    else:
        sent, scol = "BULLISH", GREEN
    if rel60[0] in ("STRETCH-DN", "STRETCH-UP") and mag != "WEAK":
        sent += " · STRETCHED"

    L = []
    drift = mp - open_mp
    L.append(f"Max pain <b>{mp:,.0f}</b> · opened the session at {open_mp:,.0f} "
             f"(<b style='color:{GREEN if drift > 0 else RED if drift < 0 else WHITE}'>{drift:+,.0f}</b>)"
             + (f" · previous session ended at {prev_mp:,.0f} (overnight {open_mp - prev_mp:+,.0f})" if np.isfinite(prev_mp) else "")
             + f" · {len(sh)} shift(s) today: {n_up} up, {n_dn} down.")
    L.append(f"Spot {spot:,.1f} is <b style='color:{scol}'>{abs(gap):,.0f} pts {'BELOW' if gap < 0 else 'ABOVE'}</b> max pain "
             f"(future {fut:,.1f} → {gap_f:+,.0f}). <span style='color:{MUTED}'>We read SPOT vs max pain: expiry settles on spot, "
             f"and the future carries a carry premium that can make it look 'above' when spot is not.</span>")
    L.append(f"<b>Last 60 min:</b> {rel60[1]}.")
    if np.isfinite(strad):
        L.append(f"<b>Reach:</b> ATM straddle {strad:,.0f} pts ≈ the move priced to expiry. The gap is "
                 f"<b>{abs(gap) / strad * 100:,.0f}%</b> of it — "
                 + ("easily within reach." if abs(gap) < 0.4 * strad else "reachable but needs a move." if abs(gap) < strad
                    else "larger than the priced move — max pain is unlikely to be reached; ignore it for targets."))
    L.append(f"<b>Magnet strength: <span style='color:{GREEN if mag == 'STRONG' else GOLD if mag == 'MODERATE' else MUTED}'>"
             f"{mag} ({sc}/100)</span></b> — {mag_why}. Max pain pulls hardest on expiry day after ~13:00; with "
             f"{dte if dte is not None else '?'} day(s) left it is a <i>map of where writers are comfortable</i>, not a target.")
    side = "DOWN" if dn > up else "UP"
    f_ = lambda v: (f"<b style='color:{RED}'>+₹{v:,.1f} B</b> (writers lose more)" if v > 0 else
                    f"<b style='color:{GREEN}'>−₹{abs(v):,.1f} B</b> (writers gain)")
    L.append(f"<b>Pain asymmetry</b> — change in what writers owe buyers for a 100-pt move from here: "
             f"down 100 → {f_(dn)} · up 100 → {f_(up)}. Writers are most exposed to a move <b>{side}</b>: "
             f"they defend that side (their hedging slows it), but a clean break makes them cover and adds fuel "
             f"{'lower' if side == 'DOWN' else 'higher'}."
             + (f" A move {'UP' if side == 'DOWN' else 'DOWN'} actually pays writers — expect them to lean on it."
                if min(up, dn) < 0 else ""))
    L.append(f"<b>Magnet zone</b> (total pain within 10% of the minimum): {now_row.zlo:,.0f} – {now_row.zhi:,.0f}"
             f"{' — a wide, flat bottom: weak pin' if now_row.zhi - now_row.zlo >= 6 * step else ''}. "
             f"OI centre of gravity {now_row.cog:,.0f} · biggest call wall {now_row.ce_wall:,.0f} · put wall {now_row.pe_wall:,.0f}.")
    inside = now_row.pe_wall <= mp <= now_row.ce_wall
    L.append("Max pain sits <b>between</b> the put wall and the call wall → writers' range is "
             f"{now_row.pe_wall:,.0f}–{now_row.ce_wall:,.0f}." if inside else
             "Max pain is <b>outside</b> the wall range → the OI map is lopsided; expect max pain to keep shifting.")

    # what to do / not do
    do, dont = [], []
    if mag == "WEAK":
        do.append("Use max pain only as BIAS: trade in the direction max pain is moving (it has moved "
                  + ("down" if n_dn > n_up else "up" if n_up > n_dn else "both ways" if n_up else "not at all")
                  + " today), with your price-action trigger.")
        dont.append("Don't buy/sell expecting price to 'go to max pain' — with days left it moves to price, not the other way.")
    else:
        do.append(f"Expect drift toward {mp:,.0f} / the zone {now_row.zlo:,.0f}–{now_row.zhi:,.0f} if price stalls; "
                  "fade only with a reversal candle at your level and a tight SL.")
        dont.append("Don't hold far-OTM options against the magnet late on expiry day — they go to ~0.")
    if rel60[0] == "BEAR-ACCEPT":
        do.append("Writers are moving lower WITH price → favour PE on pull-backs, not CE bottom-fishing.")
    elif rel60[0] == "BULL-ACCEPT":
        do.append("Writers are moving higher WITH price → favour CE on dips, not PE top-picking.")
    elif rel60[0].startswith("STRETCH"):
        do.append("Price ran away from an unmoved max pain → book profits faster; the snap-back risk is real near expiry.")
    dont.append("Don't use max pain alone — confirm with Smart OI / writers flow and your 5-rule setup. 1 lot, SL at entry.")
    return dict(lines=L, do=do, dont=dont, sent=sent, scol=scol, mag=mag, score=sc, gap=gap, mp=mp, spot=spot,
                fut=fut, dte=dte, rel=rel60, shifts=sh)


def day_story(h: pd.DataFrame, rd: dict, s_today: pd.DataFrame) -> str:
    if h.empty:
        return "Not enough data for a day narrative yet."
    codes = h.code.tolist()
    acc_dn, acc_up = codes.count("BEAR-ACCEPT"), codes.count("BULL-ACCEPT")
    st_ = sum(c.startswith("STRETCH") for c in codes)
    pin = codes.count("PIN")
    d_f = float(s_today.fut.iloc[-1] - s_today.fut.iloc[0])
    d_m = float(s_today.mp.iloc[-1] - s_today.mp.iloc[0])
    if acc_dn >= max(2, acc_up + 1):
        kind = "a <b style='color:#ef4444'>writer-accepted DOWN day</b> — max pain was walked lower hour after hour"
    elif acc_up >= max(2, acc_dn + 1):
        kind = "a <b style='color:#22c55e'>writer-accepted UP day</b> — max pain was walked higher hour after hour"
    elif pin >= len(codes) / 2:
        kind = "a <b style='color:#d4af37'>pinned / range day</b> — max pain and price mostly still"
    elif st_ >= 2:
        kind = "a <b>stretched day</b> — price moved but writers did not move their book (mean-reversion risk into expiry)"
    else:
        kind = "a <b>mixed day</b> — no clean writer story"
    return (f"So far this is {kind}. Future {d_f:+,.0f} pts, max pain {d_m:+,.0f} pts since the first snapshot "
            f"({acc_dn} accept-down, {acc_up} accept-up, {pin} pinned, {st_} stretched hour(s)). "
            f"Now: {rd['sent'].lower()}, magnet {rd['mag'].lower()}.")


# ═══════════════════════════════════════════════════════════════════════════
# 3 · Charts
# ═══════════════════════════════════════════════════════════════════════════
def chart_line(s: pd.DataFrame, sh: pd.DataFrame, fut_label: str, now_ts) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=s.ts, y=s.zhi, line=dict(width=0, shape="hv"), hoverinfo="skip", showlegend=False))
    fig.add_trace(go.Scatter(x=s.ts, y=s.zlo, fill="tonexty", fillcolor="rgba(249,115,22,0.10)", line=dict(width=0, shape="hv"),
                             name="Magnet zone", hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=s.ts, y=s.fut, name=fut_label, line=dict(color=FUT, dash="dot", width=1.4),
                             hovertemplate="%{x|%d %b %H:%M}<br>" + fut_label + " %{y:,.1f}<extra></extra>"))
    fig.add_trace(go.Scatter(x=s.ts, y=s.mp, name="Max Pain", line=dict(color=ORANGE, width=2.2, shape="hv"),
                             hovertemplate="Max Pain %{y:,.0f}<extra></extra>"))
    if len(sh):
        fig.add_trace(go.Scatter(x=sh.ts, y=sh.mp, mode="markers", name="Shift",
                                 marker=dict(symbol=["triangle-up" if a > b else "triangle-down" for a, b in zip(sh.mp, sh["from"])],
                                             color=[GREEN if a > b else RED for a, b in zip(sh.mp, sh["from"])], size=10,
                                             line=dict(color=WHITE, width=0.5)),
                                 customdata=np.c_[sh["from"], sh.fut],
                                 hovertemplate="%{x|%H:%M} max pain %{customdata[0]:,.0f} → %{y:,.0f}<br>future "
                                               "%{customdata[1]:,.1f}<extra></extra>"))
    last = s.iloc[-1]
    for y, txt, col in ((last.mp, f"{last.mp:,.0f}", ORANGE), (last.fut, f"{last.fut:,.1f}", "#64748b")):
        fig.add_annotation(x=1.0, xref="paper", y=y, text=f"<b>{txt}</b>", showarrow=False, xanchor="left",
                           bgcolor=col, font=dict(color=WHITE, size=11))
    days = sorted(s.ts.dt.normalize().unique())
    for d in days[1:]:
        fig.add_vline(x=pd.Timestamp(d) + pd.Timedelta(hours=9, minutes=15), line=dict(color="#3b82f6", dash="dot", width=1))
    if now_ts is not None and now_ts < s.ts.iloc[-1]:
        fig.add_vline(x=now_ts, line=dict(color=GOLD, dash="dash", width=1))
    ol._layout(fig, "Max Pain vs " + fut_label, h=430)
    fig.update_layout(margin=dict(l=10, r=70, t=50, b=10))
    fig.update_xaxes(rangebreaks=BREAKS, tickformat="%H:%M\n%d %b")
    fig.update_yaxes(tickformat=",.0f", side="right")
    return fig


def chart_bars(f: tuple, mult: float, n: int | None, mp: float, zlo: float, zhi: float, step: float) -> go.Figure:
    ts, spot, ks, ce, pe, _, _ = f
    cp, pp = pain(ks, ce, pe)
    cpB, ppB, totB = cp * mult / 1e9, pp * mult / 1e9, (cp + pp) * mult / 1e9
    if n:
        atm = ks[int(np.argmin(np.abs(ks - spot)))]
        keep = (ks >= atm - n * step - 1e-6) & (ks <= atm + n * step + 1e-6)
    else:
        keep = np.ones(len(ks), bool)
    k = ks[keep]
    fig = go.Figure()
    fig.add_vrect(x0=zlo - step / 2, x1=zhi + step / 2, fillcolor="rgba(249,115,22,0.10)", line_width=0)
    cd = np.c_[cpB[keep], ppB[keep], totB[keep], totB[keep] * 100]
    ht = ("<b>%{x:,.0f}</b><br><span style='color:#22c55e'>Call pain</span> ₹%{customdata[0]:,.2f} B"
          "<br><span style='color:#ef4444'>Put pain</span> ₹%{customdata[1]:,.2f} B<br>Total ₹%{customdata[2]:,.2f} B "
          "(₹%{customdata[3]:,.0f} Cr)<extra></extra>")
    fig.add_trace(go.Bar(x=k, y=cpB[keep], name="Call Pain", marker_color=GREEN, customdata=cd, hovertemplate=ht))
    fig.add_trace(go.Bar(x=k, y=ppB[keep], name="Put Pain", marker_color="#f87171", customdata=cd, hovertemplate=ht))
    fig.add_trace(go.Scatter(x=k, y=totB[keep], name="Total pain", line=dict(color=GOLD, width=1.6), hoverinfo="skip"))
    top = float(max(totB[keep].max(), 1e-9))
    fig.add_vline(x=spot, line=dict(color=WHITE, dash="dot", width=1))
    fig.add_vline(x=mp, line=dict(color=ORANGE, dash="dash", width=1.4))
    fig.add_annotation(x=spot, y=top * 1.08, text=f"Spot : {spot:,.2f}", showarrow=False, bgcolor="#334155",
                       font=dict(color=WHITE, size=11))
    fig.add_annotation(x=mp, y=top * 0.98, text=f"<b>Max Pain : {mp:,.0f}</b>", showarrow=False, bgcolor="#c2410c",
                       font=dict(color=WHITE, size=11))
    ol._layout(fig, f"Max Pain · {ts:%d %b %H:%M}", h=470)
    fig.update_layout(barmode="group", bargap=0.25, hovermode="closest")
    fig.update_yaxes(title_text="₹ B paid to option buyers at expiry", ticksuffix="B", tickformat=",.0f")
    fig.update_xaxes(tickformat=",.0f", dtick=step * 2 if len(k) <= 30 else step * 4)
    return fig


def chart_gap(s: pd.DataFrame) -> go.Figure:
    g = s.spot - s.mp
    half = (s.straddle / 2).where(s.straddle > 0)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=s.ts, y=half, line=dict(width=0), hoverinfo="skip", showlegend=False))
    fig.add_trace(go.Scatter(x=s.ts, y=-half, fill="tonexty", fillcolor="rgba(212,175,55,0.08)", line=dict(width=0),
                             name="± ½ straddle (easy reach)", hoverinfo="skip"))
    fig.add_trace(go.Bar(x=s.ts, y=g, name="Spot − Max Pain", marker_color=np.where(g >= 0, GREEN, RED),
                         hovertemplate="%{x|%d %b %H:%M}<br>spot − max pain %{y:+,.0f} pts<extra></extra>"))
    ol._layout(fig, "Distance to the magnet (spot − max pain)", h=300)
    fig.update_xaxes(rangebreaks=BREAKS, tickformat="%H:%M\n%d %b")
    fig.update_yaxes(tickformat="+,.0f", zeroline=True, zerolinecolor=WHITE)
    return fig


# ═══════════════════════════════════════════════════════════════════════════
# 4 · Streamlit
# ═══════════════════════════════════════════════════════════════════════════
HOW = [
    "<b>Max pain</b> = the expiry price at which option BUYERS collectively get paid the least (and writers keep the most). "
    "Each bar = what call buyers (green) / put buyers (red) would be paid if NIFTY settled at that strike.",
    "<b>Orange step line</b> = max pain through the day. It moves when writers add / cut OI. A ▲/▼ marks every shift.",
    "<b>Read the RELATION, not the level:</b> max pain following price (both falling) = writers accept the move → trend. "
    "Price moving while max pain stays = price stretched → snap-back risk, but mainly on expiry day.",
    "<b>Spot vs max pain</b> (not future): expiry settles on spot; the future carries a premium. Below = bearish lean, "
    "above = bullish lean, within ½ strike = pinned.",
    "<b>Magnet strength</b> grows as expiry nears — weak with 3+ days left, strongest on expiry day 13:00–15:00. "
    "With days left, max pain moves toward price far more often than price moves toward max pain.",
    "<b>Reach:</b> if the gap is bigger than the ATM straddle (the priced move to expiry), max pain is not a realistic target.",
    "<b>Pain asymmetry:</b> the side where option buyers' payout rises fastest is where writers are exposed — they defend "
    "it (hedging slows the move); a clean break of it can accelerate (short covering / hedge unwinds).",
    "<b>Magnet zone</b> (shaded) = strikes within 10% of the minimum pain. Narrow = strong pin; wide & flat = weak pin.",
    "<b>Distance chart</b> (bottom): bars inside the gold band = max pain is within easy reach; bars shrinking toward 0 "
    "into the afternoon of expiry = pinning in action.",
    "Use with Smart OI, writers flow and your price-action trigger. Not a stand-alone signal; not backtested here.",
]


def _card(title: str, body: str, border: str = GOLD) -> str:
    return (f"<div style='background:{NAVY};border:1px solid {border};border-radius:8px;padding:10px 14px;margin:6px 0'>"
            f"<div style='color:{GOLD};font-weight:700'>{title}</div>"
            f"<div style='color:{WHITE};margin-top:4px;line-height:1.6'>{body}</div></div>")


def render(day: date | None = None) -> None:
    import streamlit as st
    import store
    import config

    days = ol._days_available()
    if not days:
        st.info("No option-chain snapshots stored yet.")
        return
    c = st.columns([1.6, 1.6, 1.4, 1.6, 1.2, 1.6])
    day = c[0].selectbox("Session", days, index=days.index(day) if day in days else 0,
                         format_func=lambda d: d.strftime("%a %d %b %Y"), key="mxp_day")

    @st.cache_data(ttl=60, show_spinner=False)
    def _load(d: date, n_files: int):
        return store.load_day(d)

    def _n(d):
        return len(list((Path(config.SNAPSHOT_DIR) / d.strftime("%Y-%m-%d")).glob("*.meta.json")))

    snaps = _load(day, _n(day))
    if len(snaps) < 3:
        st.warning("Not enough snapshots for this session yet.")
        return
    exps = expiries(snaps)
    if not exps:
        st.warning("No expiry information in the snapshots.")
        return
    exp = c[1].selectbox("Expiry", exps, index=0, key="mxp_exp",
                         format_func=lambda e: f"{e} ({(_exp_date(e).date() - day).days}d)")
    tf = c[2].selectbox("Timeframe", list(TF), index=0, key="mxp_tf")
    rng = c[3].radio("Strikes ± ATM", ["All", "5", "10", "20"], index=3, horizontal=True, key="mxp_rng")
    prev_on = c[4].checkbox("Prev session", value=True, key="mxp_prev")
    src = c[5].radio("Future line", ["NIFTY future", "Synth future"], horizontal=True, key="mxp_src")
    step = float(ol._rules().get("strike_step", 50) or 50)

    @st.cache_data(ttl=60, show_spinner=False)
    def _series(d: date, n_files: int, e: str):
        fr = frames(_load(d, n_files), e)
        return fr, (series(fr, step) if fr else pd.DataFrame())

    fr, s_today = _series(day, _n(day), exp)
    if s_today.empty or len(s_today) < 2:
        st.warning("Not enough snapshots for this expiry yet.")
        return
    mult, unit_note = share_mult(fr)
    fut, flabel, why = future_frame(snaps, day, src == "NIFTY future")
    s_today = add_future(s_today, fut)

    s_prev, prev_note = pd.DataFrame(), ""
    older = [d for d in days if d < day]
    if older:
        pd_ = older[0]
        fr_p, sp = _series(pd_, _n(pd_), exp)
        if len(sp):
            sn_p = _load(pd_, _n(pd_))
            fp, _, _ = future_frame(sn_p, pd_, src == "NIFTY future")
            s_prev = add_future(sp, fp)
        else:
            prev_note = f" · previous session ({pd_:%d %b}) has no {exp} data"

    times = s_today.ts.tolist()
    at_ts = st.select_slider("View at (replay the curve and the read at any time)", options=times, value=times[-1],
                             format_func=lambda t: t.strftime("%H:%M"), key="mxp_at")
    i_at = times.index(at_ts)
    now_row = s_today.iloc[i_at]
    dte = trading_days_to_expiry(day, exp)
    st.caption(f"Expiry {exp} ({dte} trading day(s) / {(_exp_date(exp).date() - day).days} calendar left) · {len(s_today)} snapshots {s_today.ts.iloc[0]:%H:%M}–"
               f"{s_today.ts.iloc[-1]:%H:%M} · {unit_note} · max pain from ALL {len(fr[i_at][2])} strikes · "
               f"future line: {why}{prev_note}")

    rd = pro_read(s_today, s_prev, now_row, day, exp, step, mult)
    upto = s_today.iloc[: i_at + 1]
    line_df = resample(pd.concat([s_prev, upto], ignore_index=True) if (prev_on and len(s_prev)) else upto, tf)
    sh_all = shifts(pd.concat([s_prev, upto], ignore_index=True) if (prev_on and len(s_prev)) else upto)
    if TF[tf] > 1:
        sh_all = sh_all[sh_all.ts >= line_df.ts.iloc[0]]

    # headline strip
    k = st.columns(5)
    k[0].metric("Max Pain", f"{rd['mp']:,.0f}", f"{rd['mp'] - upto.mp.iloc[0]:+,.0f} today")
    k[1].metric("Spot", f"{rd['spot']:,.1f}", f"{rd['gap']:+,.0f} vs max pain")
    k[2].metric(flabel, f"{rd['fut']:,.1f}", f"{rd['fut'] - rd['mp']:+,.0f} vs max pain")
    k[3].metric("Magnet", rd["mag"], f"{rd['score']}/100", delta_color="off")
    k[4].metric("Shifts today", f"{len(rd['shifts'])}", f"last 60m: {rd['rel'][0].replace('-', ' ').title()}", delta_color="off")

    st.plotly_chart(chart_line(line_df, sh_all, flabel, at_ts), use_container_width=True, key="mxp_line")
    n = None if rng == "All" else int(rng)
    st.plotly_chart(chart_bars(fr[i_at], mult, n, rd["mp"], now_row.zlo, now_row.zhi, step),
                    use_container_width=True, key="mxp_bars")

    a, b = st.columns([1, 2])
    a.markdown(
        f"<div style='background:{NAVY};border:1px solid {rd['scol']};border-radius:8px;padding:14px;text-align:center'>"
        f"<div style='color:{MUTED};font-size:0.85rem'>MARKET SENTIMENT (based on max pain)</div>"
        f"<div style='color:{rd['scol']};font-size:1.6rem;font-weight:800;margin:6px 0'>{rd['sent']}</div>"
        f"<div style='color:{WHITE};font-size:0.9rem'>Spot {'below' if rd['gap'] < 0 else 'above'} max pain by "
        f"{abs(rd['gap']):,.0f} pts</div>"
        f"<div style='color:{MUTED};font-size:0.8rem;margin-top:8px'>Max Pain {rd['mp']:,.2f} · Spot {rd['spot']:,.2f}"
        f"<br>StockMojo-style (future): {'Bearish' if rd['fut'] < rd['mp'] else 'Bullish'}</div></div>",
        unsafe_allow_html=True)
    b.markdown(_card("★ PRO READ — what most traders don't notice", "<br>".join(rd["lines"])), unsafe_allow_html=True)

    h = hourly(upto, step)
    st.markdown(_card("📖 NARRATIVE FOR THE DAY", day_story(h, rd, upto)), unsafe_allow_html=True)
    if len(h):
        col = {"BEAR-ACCEPT": RED, "BULL-ACCEPT": GREEN, "PIN": GOLD, "STRETCH-DN": ORANGE, "STRETCH-UP": ORANGE,
               "MP-UP": GREEN, "MP-DN": RED}
        rows = "".join(
            f"<tr><td style='padding:4px 8px;color:{GOLD};white-space:nowrap'><b>{r.hour}</b></td>"
            f"<td style='padding:4px 8px;white-space:nowrap'>{r.fut0:,.0f} → {r.fut1:,.0f} "
            f"<span style='color:{GREEN if r.d_fut > 0 else RED}'>({r.d_fut:+,.0f})</span></td>"
            f"<td style='padding:4px 8px;white-space:nowrap'>{r.mp0:,.0f} → {r.mp1:,.0f} "
            f"<span style='color:{GREEN if r.d_mp > 0 else RED if r.d_mp < 0 else MUTED}'>({r.d_mp:+,.0f})</span>"
            f"{f' · {r.n_shift} shift' + ('s' if r.n_shift > 1 else '') if r.n_shift else ''}</td>"
            f"<td style='padding:4px 8px;white-space:nowrap'>{r.gap:+,.0f}</td>"
            f"<td style='padding:4px 8px;color:{col.get(r.code, WHITE)}'>{r.read}</td></tr>" for r in h.itertuples())
        st.markdown(
            f"<div style='background:{NAVY};border:1px solid {GOLD};border-radius:8px;padding:10px 14px;margin:6px 0;"
            f"overflow-x:auto'><div style='color:{GOLD};font-weight:700'>🕐 NARRATIVE FOR EVERY HOUR</div>"
            f"<table style='color:{WHITE};font-size:0.88rem;border-collapse:collapse;margin-top:6px;width:100%'>"
            f"<tr style='color:{MUTED};text-align:left'><th style='padding:4px 8px'>Hour</th><th style='padding:4px 8px'>"
            f"{flabel}</th><th style='padding:4px 8px'>Max pain</th><th style='padding:4px 8px'>Spot−MP</th>"
            f"<th style='padding:4px 8px'>Read</th></tr>{rows}</table></div>", unsafe_allow_html=True)

    dd = "".join(f"<li>✅ {x}</li>" for x in rd["do"]) + "".join(f"<li>⛔ {x}</li>" for x in rd["dont"])
    st.markdown(_card("WHAT TO DO WITH IT", f"<ul style='margin:0 0 0 18px'>{dd}</ul>"), unsafe_allow_html=True)
    st.plotly_chart(chart_gap(resample(upto, tf)), use_container_width=True, key="mxp_gap")

    with st.expander("Max-pain shift log & strike table"):
        sh = rd["shifts"]
        if len(sh):
            st.dataframe(pd.DataFrame({"Time": sh.ts.dt.strftime("%H:%M"), "From": sh["from"].round(0),
                                       "To": sh.mp.round(0), "Move": (sh.mp - sh["from"]).round(0),
                                       "Spot": sh.spot.round(1), flabel: sh.fut.round(1)}).iloc[::-1],
                         hide_index=True, use_container_width=True)
        else:
            st.caption("No max-pain shift yet this session.")
        ts, spot, ks, ce, pe, _, _ = fr[i_at]
        cp, pp = pain(ks, ce, pe)
        st.dataframe(pd.DataFrame({"Strike": ks.astype(int), "Call OI": (ce * mult).round(0), "Put OI": (pe * mult).round(0),
                                   "Call pain ₹B": (cp * mult / 1e9).round(2), "Put pain ₹B": (pp * mult / 1e9).round(2),
                                   "Total ₹B": ((cp + pp) * mult / 1e9).round(2)}),
                     hide_index=True, use_container_width=True, height=360)
    li = "".join(f"<li style='margin:3px 0'>{x}</li>" for x in HOW)
    st.markdown(_card("HOW TO READ MAX PAIN", f"<ul style='margin:0 0 0 18px'>{li}</ul>"), unsafe_allow_html=True)
    st.caption("Information only — not financial advice. Max-pain reads are heuristics and are not backtested in this app.")
