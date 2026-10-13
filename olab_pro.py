"""
olab_pro.py — the "★ PRO" layer for 🧪 Options Lab: how experienced option-chain readers actually use OI.

Most traders look at ONE number (PCR, total OI, a single candle's change). Experienced readers ask a sequence of
questions, and this module answers them from your stored snapshots, live, for every sub-tab:

  1. WHERE ARE THE WALLS, AND ARE THEY REAL?   floor (max put OI) / ceiling (max call OI), strength vs the next strike,
     still being BUILT (fresh writing) or being ABANDONED (unwinding)?
  2. ARE THE WALLS MOVING?   floor stepping up = bullish; ceiling stepping down = bearish. ROLLS: OI leaving one strike and
     appearing at the next one (writers moving their line) — the earliest sign the range is shifting.
  3. WHO IS WRITING NOW?   Writers balance = (Δput − Δcall) ÷ (|Δput| + |Δcall|) over 15 / 30 min near the money
     (−1 = only call writing … +1 = only put writing). A FLIP of this balance is the trend-change alarm.
  4. HOW FAR CAN IT GO?   ATM straddle = the move the market has priced in. Intraday expected move = straddle ×
     √(minutes left today ÷ minutes to expiry). Used/priced ratio tells the DAY TYPE (trend day vs range day).
  5. WHAT DO DEALERS DO?   gamma flip: above it hedging dampens moves (fade extremes), below it hedging amplifies them.
  6. IS PREMIUM CHEAP OR DEAR?   IV vs its own history and vs realised volatility.
  7. → TRADE FILTER: CE-buy / PE-buy graded ✅ / ⚠ / ⛔ with the reasons, so you skip the trades writers are against.

Nothing here is a proven edge yet — every rule is listed so you can test it: backtest_olab_pro.py.
OI unit = options_lab.oi_multiplier. Information only — not financial advice.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go

import config
import options_lab_tab as ol
from options_lab_tab import fmt, NAVY, GOLD, WHITE, MUTED, GREEN, RED, BLUE, PURPLE

STEP = float(getattr(config, "STRIKE_STEP", 50))
NEAR = 5                     # strikes each side of ATM used for the writers balance
BAL_ON, BAL_OFF = 0.25, 0.05  # regime enters at ±0.25, leaves when it falls back inside ±0.05 (hysteresis)


# ═══════════════════════════════════════════════════════════════════════════
# 1 · data
# ═══════════════════════════════════════════════════════════════════════════
def panel(snaps: list, mult: float) -> pd.DataFrame:
    """Long frame: one row per (snapshot, strike) — OI, change vs yesterday, LTP, IV for both sides."""
    parts = []
    for ts, spot, df in snaps:
        if df is None or df.empty:
            continue
        d = ol._one_expiry(df)
        g = lambda c: pd.to_numeric(d[c], errors="coerce") if c in d else pd.Series(np.nan, index=d.index)
        parts.append(pd.DataFrame({
            "ts": pd.Timestamp(ts), "spot": float(spot), "strike": g("strike").round(),
            "ce_oi": g("ce_oi") * mult, "pe_oi": g("pe_oi") * mult,
            "ce_chg": g("ce_chg_oi") * mult, "pe_chg": g("pe_chg_oi") * mult,
            "ce_ltp": g("ce_ltp"), "pe_ltp": g("pe_ltp"), "ce_iv": g("ce_iv"), "pe_iv": g("pe_iv"),
            "expiry": str(d["expiry"].iloc[0]) if "expiry" in d and len(d) else ""}))
    if not parts:
        return pd.DataFrame()
    p = pd.concat(parts, ignore_index=True).dropna(subset=["strike"])
    p["ts"] = pd.to_datetime(p["ts"]).astype("datetime64[ns]")
    p["strike"] = p["strike"].astype(int)
    for s in ("ce", "pe"):                                   # no change column → change vs first snapshot
        if p[f"{s}_chg"].isna().all():
            first = p.sort_values("ts").groupby("strike")[f"{s}_oi"].transform("first")
            p[f"{s}_chg"] = p[f"{s}_oi"] - first
    return p


def at(p: pd.DataFrame, ts) -> pd.DataFrame:
    """Chain at the last snapshot ≤ ts."""
    t = p.loc[p["ts"] <= ts, "ts"].max()
    if pd.isna(t):
        t = p["ts"].min()
    return p[p["ts"] == t].set_index("strike").sort_index()


def flow(p: pd.DataFrame, n: int = NEAR) -> pd.DataFrame:
    """Per snapshot: near-the-money (ATM ± n, ATM of THAT snapshot) total call / put OI."""
    rows = []
    for ts, g in p.groupby("ts", sort=True):
        spot = float(g["spot"].iloc[0])
        atm = round(spot / STEP) * STEP
        x = g[(g["strike"] - atm).abs() <= n * STEP]
        rows.append((ts, spot, float(x["ce_oi"].sum()), float(x["pe_oi"].sum())))
    f = pd.DataFrame(rows, columns=["ts", "spot", "ce", "pe"])
    return f


def balance(f: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Writers balance over a rolling window: (Δput − Δcall) / (|Δput| + |Δcall|), plus the window's Δs."""
    x = f.set_index("ts").sort_index()
    # value at or before ts − window
    prev = pd.merge_asof(x.reset_index()[["ts"]].assign(t0=lambda d: d["ts"] - pd.Timedelta(minutes=minutes)),
                         x.reset_index()[["ts", "ce", "pe"]].rename(columns={"ts": "t0"}), on="t0", direction="backward")
    out = x.reset_index()
    out["dce"] = out["ce"].values - prev["ce"].values
    out["dpe"] = out["pe"].values - prev["pe"].values
    gross = out["dce"].abs() + out["dpe"].abs()
    out["bal"] = (out["dpe"] - out["dce"]) / gross.replace(0, np.nan)
    out["gross"] = gross
    out["px"] = out["spot"].values - pd.merge_asof(
        out[["ts"]].assign(t0=lambda d: d["ts"] - pd.Timedelta(minutes=minutes)),
        x.reset_index()[["ts", "spot"]].rename(columns={"ts": "t0"}), on="t0", direction="backward")["spot"].values
    return out


def regimes(b: pd.DataFrame) -> pd.DataFrame:
    """Hysteresis regime on the balance: BULL / BEAR / BAL / UNWIND, and the flip points."""
    reg, cur = [], "BAL"
    med = b["gross"].expanding().median()
    for v, g, m, dce, dpe in zip(b["bal"], b["gross"], med, b["dce"], b["dpe"]):
        if pd.isna(v):
            reg.append(cur); continue
        if dce < 0 and dpe < 0 and g > 1.5 * (m or 0):
            cur = "UNWIND"
        elif v >= BAL_ON:
            cur = "BULL"
        elif v <= -BAL_ON:
            cur = "BEAR"
        elif abs(v) <= BAL_OFF or cur == "UNWIND":
            cur = "BAL"
        reg.append(cur)
    b = b.copy()
    b["regime"] = reg
    b["flip"] = b["regime"].ne(b["regime"].shift()) & b.index.to_series().gt(0)
    # TREND flip = the writers' DIRECTION changed (BULL ↔ BEAR), ignoring quiet BAL / UNWIND spells in between
    d = b["regime"].map({"BULL": 1.0, "BEAR": -1.0}).ffill()          # numeric → no pandas FutureWarning
    # FIX-4: a direction change counts only after the new side has held ~15 min (marker sits on the CONFIRM candle,
    # so there is no look-ahead). Today's 9 flips between 10:30 and 13:40 were chop that reversed inside 15 min.
    try:
        bar_min = max(1.0, float(pd.to_datetime(b["ts"]).diff().dt.total_seconds().median()) / 60)
    except Exception:                                                   # noqa: BLE001
        bar_min = 3.0
    hold = max(2, int(round(15 / bar_min)))
    conf, cur_, run, last = [], np.nan, 0, np.nan
    for v in d.values:
        if pd.isna(v):
            conf.append(cur_); continue
        run = run + 1 if v == last else 1
        last = v
        if pd.isna(cur_) or (v != cur_ and run >= hold):
            cur_ = v
        conf.append(cur_)
    cd = pd.Series(conf, index=b.index)
    b["tflip"] = cd.ne(cd.shift()) & cd.notna() & cd.shift().notna()
    b["regime_confirmed"] = cd.map({1.0: "BULL", -1.0: "BEAR"})
    return b


def walls(c: pd.DataFrame, spot: float, n: int = 10) -> dict:
    """Floor / ceiling within ATM ± n: strike, OI, strength (× next biggest), distance."""
    atm = round(spot / STEP) * STEP
    x = c[(c.index >= atm - n * STEP) & (c.index <= atm + n * STEP)]
    out = {}
    for side, key in (("pe", "floor"), ("ce", "ceiling")):
        xx = x[x.index <= spot + STEP / 2] if key == "floor" else x[x.index >= spot - STEP / 2]
        s = xx[f"{side}_oi"].dropna().sort_values(ascending=False)
        if len(s) < 2:
            out[key] = None; continue
        k, v = int(s.index[0]), float(s.iloc[0])
        out[key] = dict(strike=k, oi=v, strength=v / float(s.iloc[1]) if s.iloc[1] else np.nan,
                        dist=spot - k, second=int(s.index[1]))
    return out


def wall_table(p: pd.DataFrame, now, spot: float, k: int = 3, back_min: int = 30) -> pd.DataFrame:
    """Top-k call and put strikes with OI now, vs yesterday, last 30 min, status."""
    c1, c0 = at(p, now), at(p, now - pd.Timedelta(minutes=back_min))
    open_ = at(p, p["ts"].min())
    rows = []
    for side, lab in (("ce", "CALL"), ("pe", "PUT")):
        top = c1[f"{side}_oi"].dropna().sort_values(ascending=False).head(k)
        nxt = c1[f"{side}_oi"].dropna().sort_values(ascending=False)
        for kk, v in top.items():
            d30 = v - float(c0[f"{side}_oi"].get(kk, np.nan))
            dday = float(c1[f"{side}_chg"].get(kk, np.nan))
            dopen = v - float(open_[f"{side}_oi"].get(kk, np.nan))
            rel = d30 / v if v else 0
            status = ("BUILDING" if rel > 0.03 else "UNWINDING" if rel < -0.03 else "HOLDING")
            rows.append(dict(side=lab, strike=int(kk), oi=v, d30=d30, dday=dday, dopen=dopen, status=status,
                             dist=spot - kk, share=v / float(nxt.sum()) if nxt.sum() else np.nan))
    return pd.DataFrame(rows)


def rolls(p: pd.DataFrame, now, minutes: int = 30, min_frac: float = 0.08) -> list[str]:
    """Writers moving their line: OI falls at strike K and rises at a nearby strike (±1–3 steps) on the same side."""
    c1, c0 = at(p, now), at(p, now - pd.Timedelta(minutes=minutes))
    out = []
    for side, lab in (("ce", "Call"), ("pe", "Put")):
        d = (c1[f"{side}_oi"] - c0[f"{side}_oi"]).dropna()
        big = float(c1[f"{side}_oi"].max() or 0)
        if not big:
            continue
        for k, v in d[d < -min_frac * big].items():
            for j in (1, -1, 2, -2, 3, -3):
                kk = k + j * STEP
                if kk in d.index and d[kk] > 0.6 * abs(v):
                    up = kk > k
                    meaning = ("bullish — ceiling lifting" if side == "ce" and up else
                               "bearish — ceiling pressing down" if side == "ce" else
                               "bullish — floor rising" if up else "bearish — floor giving way")
                    out.append(f"{lab} writers rolled {'UP' if up else 'DOWN'} {int(k):,} → {int(kk):,} "
                               f"({fmt(v)} / +{fmt(d[kk])}) — {meaning}")
                    break
    return out


def max_pain(c: pd.DataFrame) -> float | None:
    ks = c.index.to_numpy(float)
    if not len(ks):
        return None
    ce, pe = c["ce_oi"].fillna(0).to_numpy(), c["pe_oi"].fillna(0).to_numpy()
    pain = [(np.maximum(k - ks, 0) * ce).sum() + (np.maximum(ks - k, 0) * pe).sum() for k in ks]
    return float(ks[int(np.argmin(pain))])


def straddle(c: pd.DataFrame, spot: float) -> tuple[float, float] | None:
    atm = round(spot / STEP) * STEP
    if atm not in c.index:
        return None
    v = float(c.at[atm, "ce_ltp"] + c.at[atm, "pe_ltp"])
    return (v, atm) if np.isfinite(v) and v > 0 else None


def minutes_to_expiry(now, expiry: str) -> float:
    e = pd.to_datetime(expiry, errors="coerce", dayfirst=not str(expiry)[:4].isdigit())
    if pd.isna(e):
        return np.nan
    end = pd.Timestamp(e.date()) + pd.Timedelta(hours=15, minutes=30)
    # trading minutes only: 375 per remaining session
    days = np.busday_count(pd.Timestamp(now).date(), e.date())
    today_left = max(0.0, (pd.Timestamp(now).normalize() + pd.Timedelta(hours=15, minutes=30) - pd.Timestamp(now)).total_seconds() / 60)
    return float(today_left + 375 * max(0, days)) if pd.Timestamp(now) <= end else 0.0


def expected_move(c: pd.DataFrame, spot: float, now, expiry: str) -> dict | None:
    """Straddle → move priced to expiry and for the rest of today (√time scaling)."""
    s = straddle(c, spot)
    if not s:
        return None
    m_exp = minutes_to_expiry(now, expiry)
    tl = max(0.0, (pd.Timestamp(now).normalize() + pd.Timedelta(hours=15, minutes=30) - pd.Timestamp(now)).total_seconds() / 60)
    today = s[0] * math.sqrt(tl / m_exp) if m_exp and m_exp > 0 else np.nan
    # ATM straddle ≈ the AVERAGE size of the move to expiry (≈ 0.8 σ); scaled by √time for the rest of today
    return dict(straddle=s[0], atm=s[1], to_expiry=s[0], today_left=today, mins_left=tl, mins_exp=m_exp)


def day_type(p: pd.DataFrame) -> dict | None:
    """Range NIFTY has travelled so far vs the range the OPENING straddle priced for the same elapsed time.
    Expected high–low range ≈ 2 × straddle × √(elapsed ÷ minutes to expiry)  (Brownian range ≈ 2 × mean |move|)."""
    t0, t1 = p["ts"].min(), p["ts"].max()
    c0 = at(p, t0)
    sp0 = float(p.loc[p["ts"] == t0, "spot"].iloc[0])
    s = straddle(c0, sp0)
    m_exp = minutes_to_expiry(t0, str(p["expiry"].iloc[0]))
    elapsed = max(1.0, (t1 - t0).total_seconds() / 60)
    if not s or not m_exp or not np.isfinite(m_exp) or m_exp <= 0:
        return None
    implied = 2 * s[0] * math.sqrt(min(elapsed, m_exp) / m_exp)
    spots = p.groupby("ts")["spot"].first()
    rng = float(spots.max() - spots.min())
    r = rng / implied if implied else np.nan
    kind = ("TREND / BUYERS' DAY — moving faster than the options priced" if r >= 1.3 else
            "NORMAL — moving about as priced" if r >= 0.7 else
            "RANGE / SELLERS' DAY — moving slower than priced, decay winning")
    return dict(implied=implied, realised=rng, ratio=r, kind=kind, straddle_open=s[0])


def gamma_state(df_latest: pd.DataFrame, spot: float) -> dict | None:
    try:
        g = ol.gex_by_strike(df_latest, spot)
    except Exception:                                           # noqa: BLE001
        g = None
    if g is None or g.empty:
        return None
    flip = ol.gamma_flip(g)
    pin = float(g.loc[g["net_gex"].idxmax(), "strike"])
    accel = float(g.loc[g["net_gex"].idxmin(), "strike"])
    if flip is None:
        reg = "positive" if g["net_gex"].sum() > 0 else "negative"
    else:
        reg = "positive" if spot >= flip else "negative"
    return dict(flip=flip, pin=pin, accel=accel, regime=reg, total=float(g["net_gex"].sum()))


def iv_state(p: pd.DataFrame, spot: float) -> dict | None:
    try:
        h = _iv_hist()
    except Exception:                                           # noqa: BLE001
        h = None
    c1, c0 = at(p, p["ts"].max()), at(p, p["ts"].min())
    atm = round(spot / STEP) * STEP
    iv = lambda c: float(np.nanmean([c["ce_iv"].get(atm, np.nan), c["pe_iv"].get(atm, np.nan)]))
    now_iv, open_iv = iv(c1), iv(c0)
    out = dict(iv=now_iv, iv_open=open_iv, rank=np.nan, pct=np.nan, hv=np.nan)
    if h is not None and len(h) >= 5:
        x = h["iv"].dropna().tail(60)
        if len(x) >= 5 and np.isfinite(now_iv):
            out["rank"] = 100 * (now_iv - x.min()) / (x.max() - x.min()) if x.max() > x.min() else 50.0
            out["pct"] = 100 * float((x < now_iv).mean())
        if "hv" in h and h["hv"].notna().any():
            out["hv"] = float(h["hv"].dropna().iloc[-1])
    return out


_IVH: dict = {}


def _iv_hist():
    import time as _t
    if "t" in _IVH and _t.time() - _IVH["t"] < 900:
        return _IVH["h"]
    h = ol.iv_hv_daily()
    _IVH.update(t=_t.time(), h=h)
    return h


# ═══════════════════════════════════════════════════════════════════════════
# 2 · the read + the trade filter
# ═══════════════════════════════════════════════════════════════════════════
def assess(snaps: list, mult: float, p: pd.DataFrame | None = None, with_iv: bool = True) -> dict | None:
    p = panel(snaps, mult) if p is None else p
    if p.empty or p["ts"].nunique() < 3:
        return None
    now = p["ts"].max()
    c = at(p, now)
    spot = float(p.loc[p["ts"] == now, "spot"].iloc[0])
    expiry = str(p["expiry"].iloc[-1])
    f = flow(p)
    b15, b30 = regimes(balance(f, 15)), regimes(balance(f, 30))
    w_now = walls(c, spot)
    w_30 = walls(at(p, now - pd.Timedelta(minutes=30)), spot)
    w_open = walls(at(p, p["ts"].min()), spot)
    flips30 = b30[b30["tflip"]]
    last_flip = flips30.iloc[-1] if len(flips30) else None
    em = expected_move(c, spot, now, expiry)
    dt = day_type(p)
    gm = gamma_state(snaps[-1][2], spot)
    ivs = iv_state(p, spot) if with_iv else None
    e = pd.to_datetime(expiry, errors="coerce", dayfirst=not expiry[:4].isdigit())
    exp_day = bool(pd.notna(e) and e.date() == now.date())
    a = dict(p=p, now=now, spot=spot, expiry=expiry, exp_day=exp_day, b15=b15, b30=b30, walls=w_now, walls30=w_30,
             walls_open=w_open, last_flip=last_flip, em=em, day=dt, gamma=gm, iv=ivs,
             rolls=rolls(p, now), mp_now=max_pain(c), mp_open=max_pain(at(p, p["ts"].min())))
    a["filter"] = trade_filter(a)
    return a


def _shift(w_now, w_old, key) -> int:
    if not w_now.get(key) or not w_old.get(key):
        return 0
    return int(np.sign(w_now[key]["strike"] - w_old[key]["strike"]))


def trade_filter(a: dict) -> dict:
    """Grade CE-buy and PE-buy: score from writer evidence; ⛔ vetoes for the classic losing setups."""
    out = {}
    b30 = a["b30"].iloc[-1]
    b15 = a["b15"].iloc[-1]
    reg30, reg15 = b30["regime"], b15["regime"]
    spot, w = a["spot"], a["walls"]
    em = a["em"]
    room_unit = (em["today_left"] if em and np.isfinite(em["today_left"]) else 40.0) or 40.0
    for side in ("CE", "PE"):
        bull = side == "CE"
        good, bad, veto = [], [], []
        sgn = 1 if bull else -1
        # writers balance (30 m main, 15 m confirm)
        if reg30 == ("BULL" if bull else "BEAR"):
            good.append(f"30-min writers {'put' if bull else 'call'}-heavy (balance {b30['bal']:+.2f})")
        elif reg30 == ("BEAR" if bull else "BULL"):
            bad.append(f"30-min writers against you (balance {b30['bal']:+.2f})")
        if reg15 == ("BEAR" if bull else "BULL") and reg30 != ("BEAR" if bull else "BULL"):
            bad.append("15-min balance just turned against you")
        if reg15 == ("BULL" if bull else "BEAR"):
            good.append("15-min writers agree")
        # walls moving
        fs, cs = _shift(w, a["walls30"], "floor"), _shift(w, a["walls30"], "ceiling")
        if bull and fs > 0 or (not bull and cs < 0):
            good.append(f"{'floor stepped UP' if bull else 'ceiling stepped DOWN'} in 30 min")
        if bull and fs < 0 or (not bull and cs > 0):
            bad.append(f"{'floor stepped DOWN' if bull else 'ceiling stepped UP'} in 30 min")
        for r in a["rolls"]:
            if ("bullish" in r) == bull:
                good.append(r.split(" — ")[0])
            else:
                bad.append(r.split(" — ")[0])
        # room to the opposing wall
        opp = w.get("ceiling" if bull else "floor")
        if opp:
            room = (opp["strike"] - spot) * sgn
            if 0 <= room < 0.35 * room_unit and opp["strength"] >= 1.3:
                veto.append(f"buying INTO a strong {'ceiling' if bull else 'floor'} {opp['strike']:,} "
                            f"({room:.0f} pts away, {opp['strength']:.1f}× next strike)")
            elif room >= room_unit:
                good.append(f"room to the {'ceiling' if bull else 'floor'}: {room:.0f} pts (≥ today's expected move)")
        own = w.get("floor" if bull else "ceiling")
        if own and own["strength"] >= 1.3 and 0 <= (spot - own["strike"]) * sgn <= 0.5 * room_unit:
            good.append(f"close to a strong {'floor' if bull else 'ceiling'} {own['strike']:,} — tight stop possible")
        # day / time context
        if a["exp_day"] and pd.Timestamp(a["now"]).time() >= time(13, 30):
            veto.append("expiry afternoon — theta crush & unwinding, OI direction unreliable")
        if reg30 == "UNWIND":
            bad.append("both sides unwinding — no writer support either way")
        if a["day"] and a["day"]["ratio"] >= 1.8:
            bad.append(f"day already stretched ({a['day']['ratio']:.1f}× the range priced at the open) — late-entry risk")
        if a["day"] and a["day"]["ratio"] < 0.5 and pd.Timestamp(a["now"]).time() >= time(11, 30):
            bad.append("range day so far — option buys bleed to decay unless price breaks a wall")
        g = a["gamma"]
        if g and g["regime"] == "positive" and opp and abs(opp["strike"] - spot) < room_unit:
            bad.append("positive gamma — dealers dampen moves, breakouts struggle")
        iv = a["iv"]
        if iv and np.isfinite(iv.get("rank", np.nan)) and iv["rank"] >= 80:
            bad.append(f"IV rank {iv['rank']:.0f} — premiums dear, IV crush risk for buyers")
        score = len(good) - len(bad)
        grade = "⛔ AVOID" if veto else "✅ FAVOURED" if score >= 2 else "⚠ CAUTION" if score >= 0 else "⛔ AVOID"
        out[side] = dict(grade=grade, good=good, bad=bad + veto, score=score)
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 3 · UI helpers
# ═══════════════════════════════════════════════════════════════════════════
def _card(title: str, body: str, border: str = GOLD) -> str:
    return (f"<div style='background:{NAVY};border:1px solid {border};border-left:4px solid {border};border-radius:8px;"
            f"padding:10px 14px;margin:4px 0 10px;color:{WHITE}'><div style='color:{GOLD};font-weight:800'>{title}</div>"
            f"<div style='margin-top:4px;line-height:1.55'>{body}</div></div>")


def _c(v: float, txt: str) -> str:
    return f"<span style='color:{GREEN if v > 0 else RED if v < 0 else MUTED}'>{txt}</span>"


def _reg_html(r: str) -> str:
    col = {"BULL": GREEN, "BEAR": RED, "UNWIND": GOLD}.get(r, MUTED)
    lab = {"BULL": "PUT writers lead (bullish)", "BEAR": "CALL writers lead (bearish)", "UNWIND": "both sides UNWINDING",
           "BAL": "balanced"}.get(r, r)
    return f"<b style='color:{col}'>{lab}</b>"


_A: dict = {}


def load(day: date | None = None):
    """Snapshots + assessment for a session, cached per (day, snapshot count)."""
    import store
    days = ol._days_available()
    if not days:
        return None, None
    day = day or days[0]
    n = len(list((Path(config.SNAPSHOT_DIR) / day.strftime("%Y-%m-%d")).glob("*.meta.json")))
    key = (day, n)
    if key not in _A:
        snaps = store.load_day(day)
        _A.clear()
        _A[key] = (snaps, assess(snaps, float(ol._rules()["oi_multiplier"])) if len(snaps) >= 3 else None)
    return _A[key]


# ═══════════════════════════════════════════════════════════════════════════
# 4 · ★ PRO DECISION BRIEF (above the sub-tabs)
# ═══════════════════════════════════════════════════════════════════════════
def backtest_note() -> str | None:
    """One line from YOUR last backtest_olab_pro.py run (data/backtest_olab_pro.csv)."""
    f = Path(config.DATA_DIR) / "backtest_olab_pro.csv"
    if not f.exists():
        return None
    try:
        df = pd.read_csv(f)
        g = df.groupby("grade")["right"].agg(["mean", "size"])
        fav = next((k for k in g.index if "FAVOURED" in k), None)
        av = next((k for k in g.index if "AVOID" in k), None)
        if fav is None:
            return None
        fh, fn = 100 * g.loc[fav, "mean"], int(g.loc[fav, "size"])
        ah = 100 * g.loc[av, "mean"] if av is not None else np.nan
        edge = fh >= 55 and (not np.isfinite(ah) or fh - ah >= 5)
        verdict = ("filter showed an edge — still re-check on new sessions" if edge else
                   "NO EDGE — the grades are a checklist, NOT a signal")
        return (f"Your backtest ({df['day'].nunique()} sessions): FAVOURED right {fh:.0f}% (n={fn})"
                + (f" vs AVOID {ah:.0f}%" if np.isfinite(ah) else "") + f" → {verdict}.")
    except Exception:                                           # noqa: BLE001
        return None


def brief(day: date | None = None) -> None:
    import streamlit as st
    snaps, a = load(day)
    if not a:
        return
    with st.expander("★ PRO DECISION BRIEF — writers' range · flow · expected move · trade filter", expanded=True):
        w, w0 = a["walls"], a["walls_open"]
        spot = a["spot"]
        c1, c2, c3 = st.columns([1.15, 1, 1.1])
        # walls
        fl, ce = w.get("floor"), w.get("ceiling")
        def wl(x, x0, lab, side):
            if not x:
                return f"{lab}: —"
            mv = ""
            if x0 and x0["strike"] != x["strike"]:
                up = x["strike"] > x0["strike"]                 # either wall moving UP is bullish, DOWN bearish
                mv = " · " + _c(1 if up else -1, f"{'↑' if up else '↓'} from {x0['strike']:,} at open")
            st_ = "strong" if x["strength"] >= 1.5 else "moderate" if x["strength"] >= 1.15 else "weak (shared)"
            return (f"<b>{lab} {x['strike']:,}</b> · {fmt(x['oi'])} · {x['strength']:.1f}× next ({st_}) · "
                    f"{abs(x['dist']):.0f} pts {'below' if x['dist'] > 0 else 'above'}{mv}")
        rng = ""
        if fl and ce:
            width = ce["strike"] - fl["strike"]
            pos = (spot - fl["strike"]) / width * 100 if width > 0 else np.nan
            rng = f"<br>Writers' range <b>{fl['strike']:,} – {ce['strike']:,}</b> ({width:.0f} pts) · spot at {pos:.0f}% of it"
        mp = (f"<br>Max pain {a['mp_now']:,.0f}" + (f" (open {a['mp_open']:,.0f})" if a['mp_open'] and a['mp_open'] != a['mp_now'] else "")
              if a["mp_now"] else "")
        c1.markdown(_card("1 · WALLS", wl(fl, w0.get("floor"), "Floor", "floor") + "<br>" +
                          wl(ce, w0.get("ceiling"), "Ceiling", "ceiling") + rng + mp), unsafe_allow_html=True)
        # flow
        b15, b30 = a["b15"].iloc[-1], a["b30"].iloc[-1]
        lf = a["last_flip"]
        flip = (f"<br>Last TREND flip: <b>{lf['ts']:%H:%M}</b> → {_reg_html(lf['regime_confirmed'])}" if lf is not None else "<br>No trend flip today")
        c2.markdown(_card("2 · WHO IS WRITING (ATM ±5)",
                          f"30 min: {_reg_html(b30['regime'])} · balance {_c(b30['bal'], f'{b30.bal:+.2f}')}"
                          f"<br><span style='color:{MUTED}'>Δput {fmt(b30['dpe'])} · Δcall {fmt(b30['dce'])} · NIFTY {b30['px']:+.0f}</span>"
                          f"<br>15 min: {_reg_html(b15['regime'])} · balance {_c(b15['bal'], f'{b15.bal:+.2f}')}" + flip),
                    unsafe_allow_html=True)
        # expected move / day type / gamma / iv
        em, dt, g, iv = a["em"], a["day"], a["gamma"], a["iv"]
        bits = []
        if em:
            bits.append(f"ATM straddle <b>{em['straddle']:.1f}</b> → priced to expiry ±{em['to_expiry']:.0f} · "
                        + (f"rest of today ≈ ±{em['today_left']:.0f} pts" if em["mins_left"] >= 1 else "market closed"))
        if dt:
            bits.append(f"Day so far: range {dt['realised']:.0f} vs ≈{dt['implied']:.0f} priced "
                        f"({dt['ratio']:.1f}×) — <b>{dt['kind'].split(' — ')[0]}</b>")
        if g:
            bits.append(f"Gamma {'<b style=color:' + GREEN + '>+ve</b> (moves dampened, fade extremes)' if g['regime'] == 'positive' else '<b style=color:' + RED + '>−ve</b> (moves amplified, trends run)'}"
                        + (f" · flip {g['flip']:,.0f}" if g["flip"] else "") + f" · pin {g['pin']:,.0f}")
        if iv and np.isfinite(iv["iv"]):
            rk = f" · IV rank {iv['rank']:.0f}" if np.isfinite(iv.get("rank", np.nan)) else ""
            bits.append(f"ATM IV {iv['iv']:.1f}% (open {iv['iv_open']:.1f}%){rk}")
        c3.markdown(_card("3 · HOW FAR · WHAT KIND OF DAY", "<br>".join(bits) or "—"), unsafe_allow_html=True)
        # filter
        f = a["filter"]
        cc = st.columns(2)
        for col, side in ((cc[0], "CE"), (cc[1], "PE")):
            x = f[side]
            border = GREEN if x["grade"].startswith("✅") else RED if x["grade"].startswith("⛔") else GOLD
            li = "".join(f"<li style='color:{GREEN}'>{html_esc(r)}</li>" for r in x["good"]) + \
                 "".join(f"<li style='color:{RED}'>{html_esc(r)}</li>" for r in x["bad"])
            col.markdown(_card(f"4 · BUY {side} → {x['grade']}", f"<ul style='margin:0 0 0 18px'>{li or '<li>no evidence either way</li>'}</ul>",
                               border), unsafe_allow_html=True)
        if a["rolls"]:
            st.markdown(_card("ROLLS (last 30 min) — writers moving their line", "<br>".join(html_esc(r) for r in a["rolls"]),
                              PURPLE), unsafe_allow_html=True)
        bt = backtest_note()
        if bt:
            st.markdown(f"<div style='background:#3b0d0d;border:1px solid {RED};border-radius:8px;padding:8px 12px;"
                        f"color:{WHITE}'>⚠ {html_esc(bt)}</div>" if "NO EDGE" in bt else
                        f"<div style='color:{MUTED}'>{html_esc(bt)}</div>", unsafe_allow_html=True)
        st.caption(f"As of {a['now']:%H:%M} · expiry {a['expiry']}{' (EXPIRY DAY)' if a['exp_day'] else ''} · "
                   + ("" if bt else "rules are UNVALIDATED — run backtest_olab_pro.py before trusting a grade."))


def html_esc(s: str) -> str:
    import html as _h
    return _h.escape(str(s))


# ═══════════════════════════════════════════════════════════════════════════
# 5 · per-tab ★ PRO panels
# ═══════════════════════════════════════════════════════════════════════════
def tab(key: str, day: date | None = None) -> None:
    import streamlit as st
    try:
        snaps, a = load(day)
        if not a:
            return
        fn = {"oi": _t_oi, "gex": _t_gex, "iv": _t_iv, "writers": _t_writers, "roll": _t_roll,
              "decay": _t_decay, "flow": _t_writers}.get(key)
        if fn:
            with st.expander(f"★ PRO — {TITLES.get(key, key)}", expanded=True):
                fn(st, a, snaps)
    except Exception as exc:                                    # noqa: BLE001
        st.caption(f"★ PRO panel unavailable: {exc}")


TITLES = {"oi": "wall map: building / holding / unwinding · rolls · max-pain drift",
          "gex": "dealer gamma: regime · pin · acceleration zone · expected move",
          "iv": "is premium cheap or expensive for a BUYER today?",
          "writers": "writers balance timeline · regime flips · trade filter",
          "flow": "writers balance timeline · regime flips · trade filter",
          "roll": "rolls: writers moving their strikes",
          "decay": "priced move vs real move — what kind of day is it?"}


def _t_oi(st, a, snaps):
    t = wall_table(a["p"], a["now"], a["spot"])
    if t.empty:
        st.caption("not enough strikes"); return
    rows = ""
    for r in t.itertuples():
        col = {"BUILDING": GREEN, "UNWINDING": RED, "HOLDING": MUTED}[r.status]
        meaning = {("PUT", "BUILDING"): "floor being reinforced", ("PUT", "UNWINDING"): "floor being abandoned",
                   ("CALL", "BUILDING"): "ceiling being reinforced", ("CALL", "UNWINDING"): "ceiling being abandoned",
                   }.get((r.side, r.status), "unchanged")
        rows += (f"<tr><td style='color:{GREEN if r.side == 'CALL' else RED};font-weight:700'>{r.side}</td>"
                 f"<td><b>{r.strike:,}</b></td><td>{fmt(r.oi)}</td><td>{r.share * 100:.0f}%</td>"
                 f"<td>{_c(r.dday, fmt(r.dday))}</td><td>{_c(r.dopen, fmt(r.dopen))}</td><td>{_c(r.d30, fmt(r.d30))}</td>"
                 f"<td style='color:{col};font-weight:700'>{r.status}</td><td>{r.dist:+.0f}</td>"
                 f"<td style='color:{MUTED}'>{meaning}</td></tr>")
    st.markdown(f"<div style='background:{NAVY};border:1px solid {GOLD};border-radius:8px;padding:8px;overflow-x:auto'>"
                f"<table style='width:100%;color:{WHITE};font-size:0.85rem;border-collapse:collapse'>"
                f"<tr style='color:{GOLD}'><td>Side</td><td>Strike</td><td>OI</td><td>Share</td><td>vs yday</td>"
                f"<td>since open</td><td>last 30m</td><td>Status</td><td>Spot−K</td><td>Meaning</td></tr>{rows}</table></div>",
                unsafe_allow_html=True)
    tips = []
    if a["rolls"]:
        tips += a["rolls"]
    if a["mp_now"] and a["mp_open"] and a["mp_now"] != a["mp_open"]:
        tips.append(f"Max pain drifted {a['mp_open']:,.0f} → {a['mp_now']:,.0f} "
                    f"({'up' if a['mp_now'] > a['mp_open'] else 'down'} — writers' comfort zone moving with it)")
    st.markdown(_card("HOW EXPERIENCED READERS USE THIS",
                      ("<br>".join(html_esc(x) for x in tips) + "<br><br>" if tips else "") +
                      "• Trust a wall only if it is <b>BUILDING or HOLDING</b> and ≥ 1.5× the next strike — an UNWINDING wall "
                      "is a door, not a wall.<br>• <b>Share</b> = this strike's part of all OI on its side: one strike with "
                      "≥ 20% is a real line; many similar strikes = no clear line.<br>• A wall that keeps OI while price "
                      "presses into it (last 30m ≥ 0) is being <b>defended</b>; OI falling as price approaches = writers "
                      "covering → breakout likely.<br>• Read the <b>change bars</b> (hatched) more than the solid bars: "
                      "solid = old positions, hatched = today's decision."), unsafe_allow_html=True)


def _t_gex(st, a, snaps):
    g, em = a["gamma"], a["em"]
    if not g:
        st.caption("gamma not available in these snapshots"); return
    reg = ("<b style='color:%s'>POSITIVE gamma</b> — dealers sell rallies / buy dips → mean-reverting, pinning; "
           "fade moves into walls, take quicker profits" % GREEN if g["regime"] == "positive" else
           "<b style='color:%s'>NEGATIVE gamma</b> — dealers chase moves → breakouts travel, trends extend; "
           "don't fade, use trailing stops" % RED)
    em_txt = (f"<br>Expected move (straddle {em['straddle']:.1f}): rest of today ±{em['today_left']:.0f} pts → "
              f"<b>{a['spot'] - em['today_left']:,.0f} – {a['spot'] + em['today_left']:,.0f}</b>" if em else "")
    pin = (f"<br>Pin / magnet strike (largest +gamma): <b>{g['pin']:,.0f}</b> · acceleration zone (most −gamma): "
           f"<b>{g['accel']:,.0f}</b>" + (f" · flip <b>{g['flip']:,.0f}</b> (spot {a['spot'] - g['flip']:+.0f})" if g["flip"] else ""))
    exp = ("<br><b style='color:%s'>Expiry day:</b> after ~13:00 price is often pulled to the pin strike; a break of the "
           "acceleration zone can run fast." % GOLD if a["exp_day"] else "")
    st.markdown(_card("DEALER POSITIONING", reg + pin + em_txt + exp), unsafe_allow_html=True)


def _t_iv(st, a, snaps):
    iv = a["iv"]
    if not iv or not np.isfinite(iv["iv"]):
        st.caption("IV not available"); return
    rk, pc, hv = iv.get("rank", np.nan), iv.get("pct", np.nan), iv.get("hv", np.nan)
    verdict = ("<b style='color:%s'>EXPENSIVE</b> for buyers — need a bigger move; prefer ITM / higher delta (less vega), "
               "smaller size, or wait for IV to cool" % RED if (np.isfinite(rk) and rk >= 70) or (np.isfinite(hv) and iv["iv"] > hv * 1.25)
               else "<b style='color:%s'>CHEAP</b> for buyers — ATM/near-OTM give the most for the money; IV expansion "
               "would help you" % GREEN if (np.isfinite(rk) and rk <= 30) or (np.isfinite(hv) and iv["iv"] < hv * 0.9)
               else "<b>FAIR</b> — no IV edge either way; let price & writers decide")
    trend = iv["iv"] - iv["iv_open"]
    body = (f"ATM IV <b>{iv['iv']:.1f}%</b> ({_c(trend, f'{trend:+.1f}')} since open)"
            + (f" · IV rank (60d) <b>{rk:.0f}</b> · percentile {pc:.0f}" if np.isfinite(rk) else " · need ≥ 5 stored days for IV rank")
            + (f" · realised vol (HV) {hv:.1f}%" if np.isfinite(hv) else "") + f"<br>→ {verdict}."
            + "<br>• IV falling intraday while you hold = you lose even if price is flat → use tighter time stops."
            + "<br>• IV jumping with price falling = fear; puts get expensive fast — late PE buys are overpaid."
            + ("<br>• Expiry day: IV collapses into 15:30 by design — ignore IV direction, watch only price."
               if a["exp_day"] else ""))
    st.markdown(_card("PREMIUM: CHEAP OR DEAR?", body), unsafe_allow_html=True)


def _t_writers(st, a, snaps):
    from plotly.subplots import make_subplots
    b = a["b30"]
    b15 = a["b15"]
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Scatter(x=b["ts"], y=b["spot"], name="NIFTY", line=dict(color=WHITE, dash="dot", width=1.2),
                             hovertemplate="NIFTY %{y:,.1f}<extra></extra>"), secondary_y=False)
    fig.add_trace(go.Scatter(x=b["ts"], y=b["bal"], name="Writers balance 30m", line=dict(color=GOLD, width=2.4),
                             hovertemplate="balance 30m %{y:+.2f}<extra></extra>"), secondary_y=True)
    fig.add_trace(go.Scatter(x=b15["ts"], y=b15["bal"], name="15m", line=dict(color=BLUE, width=1.2),
                             hovertemplate="balance 15m %{y:+.2f}<extra></extra>"), secondary_y=True)
    # regime bands
    cur, start = None, None
    for ts, r in zip(list(b["ts"]) + [None], list(b["regime"]) + [None]):
        if r != cur:
            if cur in ("BULL", "BEAR", "UNWIND") and start is not None:
                fig.add_vrect(x0=start, x1=ts or b["ts"].iloc[-1], line_width=0, layer="below",
                              fillcolor={"BULL": "rgba(34,197,94,.12)", "BEAR": "rgba(239,68,68,.12)",
                                         "UNWIND": "rgba(212,175,55,.10)"}[cur])
            cur, start = r, ts
    fl = b[b["tflip"]]
    if len(fl):
        fig.add_trace(go.Scatter(x=fl["ts"], y=fl["bal"], mode="markers+text", name="Trend flip",
                                 text=[{"BULL": "▲", "BEAR": "▼", "UNWIND": "◆", "BAL": "•"}[r] for r in fl["regime_confirmed"]],
                                 textposition="top center", textfont=dict(color=GOLD, size=14),
                                 marker=dict(color=GOLD, size=7),
                                 hovertemplate="%{x|%H:%M} regime → " + "%{customdata}<extra></extra>",
                                 customdata=fl["regime_confirmed"]), secondary_y=True)
    for y in (BAL_ON, -BAL_ON):
        fig.add_hline(y=y, line=dict(color=MUTED, dash="dot", width=1), secondary_y=True)
    fig.update_yaxes(title_text="NIFTY", secondary_y=False, showgrid=False, tickformat=",.0f")
    fig.update_yaxes(title_text="balance (−1 calls … +1 puts)", range=[-1.05, 1.05], tickvals=[-1, -0.25, 0, 0.25, 1],
                     secondary_y=True)
    ol._layout(fig, "Call vs Put writers balance (ATM ±5) — green band = put writers lead, red = call writers lead", h=380)
    fig.update_layout(hovermode="x unified", legend=dict(orientation="h", x=1, xanchor="right", y=1.15))
    st.plotly_chart(fig, use_container_width=True, key=f"olabpro_bal_{len(b)}")
    f = a["filter"]
    st.markdown(_card("HOW TO USE IT TO AVOID WRONG TRADES",
                      f"Now: CE buy <b>{f['CE']['grade']}</b> · PE buy <b>{f['PE']['grade']}</b> (reasons in the brief above)."
                      "<br>• Only buy calls in a <b>green band</b> and puts in a <b>red band</b>. Most losing option buys are "
                      "taken against the writers.<br>• A <b>flip ▲/▼</b> is the trend-change alarm — the first pullback after "
                      "a flip is usually the cleanest entry; a flip that reverses within 15 min was noise."
                      "<br>• <b>Gold band (◆ unwind)</b>: both sides closing → no support either way; stand aside or "
                      "scalp only.<br>• Balance between the dotted lines = nobody in control → range; trade the walls, not "
                      "the middle."), unsafe_allow_html=True)


def _t_roll(st, a, snaps):
    rs = a["rolls"]
    body = ("<br>".join(html_esc(r) for r in rs) if rs else "No rolls in the last 30 min — walls are where they were.")
    st.markdown(_card("ROLLS — writers moving their line (last 30 min)",
                      body + "<br><br>• A roll is earlier than a wall shift: writers buy back one strike and sell the next. "
                      "<br>• Call writers rolling DOWN onto price = strong bearish intent; put writers rolling UP under price "
                      "= strong bullish intent.<br>• In the charts below, look for one strike's line falling while its "
                      "neighbour rises at the same time.", PURPLE), unsafe_allow_html=True)


def _t_decay(st, a, snaps):
    dt, em = a["day"], a["em"]
    if not dt:
        st.caption("needs the opening straddle"); return
    bar = min(100, dt["ratio"] * 100)
    col = GREEN if dt["ratio"] >= 1 else GOLD if dt["ratio"] >= 0.7 else RED
    st.markdown(_card("PRICED MOVE vs REAL MOVE",
                      f"For the time elapsed, the opening straddle ({dt['straddle_open']:.1f}) priced a high–low range of "
                      f"≈ <b>{dt['implied']:.0f} pts</b>; NIFTY has travelled <b>{dt['realised']:.0f} pts</b> ({dt['ratio']:.2f}×)."
                      f"<div style='background:#1e293b;border-radius:6px;height:10px;margin:6px 0'><div style='width:{bar:.0f}%;"
                      f"height:10px;border-radius:6px;background:{col}'></div></div><b>{dt['kind']}</b>"
                      + (f"<br>Rest of today priced ±{em['today_left']:.0f} pts." if em and np.isfinite(em["today_left"])
                         and em["mins_left"] >= 1 else "<br>Market closed for today." if em else "")
                      + "<br>• Below 0.7× by midday → sellers' day: option buyers need a catalyst; take profits fast."
                        "<br>• Above 1.3× → trend day: decay is losing to movement; hold winners with a trailing stop."),
                unsafe_allow_html=True)
