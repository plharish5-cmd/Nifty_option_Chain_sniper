"""
smc_five_rule.py — the 5-Rule SMC model (from the SMC masterclass), adapted to NIFTY.

  RULE 1  BIAS          15m EXTERNAL structure (only swings that broke structure count —
                        internal breaks inside the swing range are inducement, not a shift).
                        Daily-candle bias is added ONLY if backtest_daily_bias.py PASSED on
                        your NIFTY history (data/backtests/daily_bias.json); otherwise shown as info.
  RULE 2  TIME          inside a NIFTY trading window (rules JSON; default 09:30–11:30, 13:30–14:45)
  RULE 3  LIQUIDATION   for longs: a sell-side level swept today and reclaimed (Opening-range
                        low, Initial-balance low, previous-day low, or a 5m swing low). Shorts mirror.
  RULE 4  CONFIRMATION  price tapped a 15m/5m demand zone, then the 1m printed a bullish CHoCH
                        (lower-TF order flow turned with the higher TF). Shorts mirror.
  RULE 5  ENTRY         5m order block / FVG / inverted FVG at the shift. SL beyond the sweep/zone,
                        target = opposite 15m EXTERNAL swing, R:R ≥ min_rr (default 3).

All 5 ✔ → TAKE TRADE.  Closed candles only.  Settings: strategy1_rules.json → "smc_five_rule".
Information only — not financial advice.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from pathlib import Path

import numpy as np
import pandas as pd

import smc

ROOT = Path(__file__).parent
RULES_PATH = ROOT / "strategy1_rules.json"
BT_PATH = ROOT / "data" / "backtests" / "daily_bias.json"
_DEF = {"windows": [["09:30", "11:30"], ["13:30", "14:45"]], "opening_range_minutes": 15,
        "ib_minutes": 60, "min_rr": 3.0, "sl_buffer_pts": 5.0, "zone_tap_lookback_min": 45,
        "ltf_lookback_bars": 120, "min_pullback_bars": 3, "pivot_n": 2, "min_sl_pts": 15.0,
        "draw_min_edge_pts": 3.0, "draw_max_p": 0.01}


def _rules() -> dict:
    try:
        r = json.loads(RULES_PATH.read_text()).get("smc_five_rule", {})
    except Exception:
        r = {}
    return {**_DEF, **{k: v for k, v in r.items() if not k.startswith("_")}}


def _ts(df):
    return pd.to_datetime(df["timestamp"]) if "timestamp" in df else pd.to_datetime(df.index.to_series())


# ═══════════════════════════════════════════════════════════════════════════
# External structure (the "box method")
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class External:
    trend: str = "NONE"           # BULLISH | BEARISH | NONE
    swing_high: float | None = None
    swing_low: float | None = None
    hi_i: int | None = None
    lo_i: int | None = None
    events: list = field(default_factory=list)   # (i, "BOS"/"CHOCH", direction, level)
    sweeps: list = field(default_factory=list)   # (i, "HIGH"/"LOW", level) wick beyond, close back


def external_structure(df: pd.DataFrame, min_pullback: int = 3) -> External:
    """
    Swing high/low = the reversal points that BROKE structure. A close beyond the swing
    high is a BOS (or CHoCH if the trend was bearish); the new swing low becomes the lowest
    low between the old swing high and the break. Wicks beyond a swing without a close = sweep.
    The leg's extreme keeps extending until a pullback of ≥ min_pullback candles locks it.
    """
    h, l, c = df["high"].to_numpy(float), df["low"].to_numpy(float), df["close"].to_numpy(float)
    e = External()
    n = len(df)
    if n < 10:
        return e
    k = min(10, n)
    e.hi_i, e.lo_i = int(np.argmax(h[:k])), int(np.argmin(l[:k]))
    e.swing_high, e.swing_low = h[e.hi_i], l[e.lo_i]
    locked_hi = locked_lo = False
    for i in range(k, n):
        if c[i] > e.swing_high and (locked_hi or e.trend != "BULLISH"):
            kind = "CHOCH" if e.trend == "BEARISH" else "BOS"
            seg = slice(e.hi_i, i + 1)
            e.lo_i = e.hi_i + int(np.argmin(l[seg]))
            e.swing_low = l[e.lo_i]
            e.events.append((i, kind, "BULLISH", e.swing_high))
            e.trend, e.hi_i, e.swing_high, locked_hi, locked_lo = "BULLISH", i, h[i], False, True
            continue
        if c[i] < e.swing_low and (locked_lo or e.trend != "BEARISH"):
            kind = "CHOCH" if e.trend == "BULLISH" else "BOS"
            seg = slice(e.lo_i, i + 1)
            e.hi_i = e.lo_i + int(np.argmax(h[seg]))
            e.swing_high = h[e.hi_i]
            e.events.append((i, kind, "BEARISH", e.swing_low))
            e.trend, e.lo_i, e.swing_low, locked_lo, locked_hi = "BEARISH", i, l[i], False, True
            continue
        # extend the current leg until it is locked by a pullback
        if e.trend == "BULLISH" and not locked_hi:
            if h[i] >= e.swing_high:
                e.hi_i, e.swing_high = i, h[i]
            elif i - e.hi_i >= min_pullback:
                locked_hi = True
        if e.trend == "BEARISH" and not locked_lo:
            if l[i] <= e.swing_low:
                e.lo_i, e.swing_low = i, l[i]
            elif i - e.lo_i >= min_pullback:
                locked_lo = True
        if e.trend == "NONE":
            if h[i] > e.swing_high:
                e.hi_i, e.swing_high = i, h[i]
            if l[i] < e.swing_low:
                e.lo_i, e.swing_low = i, l[i]
        # sweeps of the protected levels
        if h[i] > e.swing_high and c[i] <= e.swing_high and locked_hi:
            e.sweeps.append((i, "HIGH", e.swing_high))
        if l[i] < e.swing_low and c[i] >= e.swing_low and locked_lo:
            e.sweeps.append((i, "LOW", e.swing_low))
    return e


# ═══════════════════════════════════════════════════════════════════════════
# Daily bias
# ═══════════════════════════════════════════════════════════════════════════
def daily_from_intraday(df: pd.DataFrame) -> pd.DataFrame:
    t = _ts(df)
    g = df.assign(day=t.dt.normalize().values).groupby("day")
    return pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                         "close": g["close"].last()}).reset_index()


def daily_bias(daily: pd.DataFrame, today) -> tuple[str, str]:
    """Bias for `today` from the last COMPLETED day vs the day before it."""
    d = daily[pd.to_datetime(daily["day"]).dt.date < today].tail(2)
    if len(d) < 2:
        return "NONE", "need 2 completed days"
    p, y = d.iloc[0], d.iloc[1]
    if y.close > p.high:
        return "BULLISH", f"yesterday closed above PDH {p.high:,.0f}"
    if y.close < p.low:
        return "BEARISH", f"yesterday closed below PDL {p.low:,.0f}"
    up, dn = y.high > p.high, y.low < p.low
    if up and not dn:
        return "BEARISH", f"yesterday wicked above PDH {p.high:,.0f} and closed back inside (rejection)"
    if dn and not up:
        return "BULLISH", f"yesterday wicked below PDL {p.low:,.0f} and closed back inside (rejection)"
    return "NONE", "inside day / swept both sides — no read"


def load_daily_file() -> pd.DataFrame | None:
    """Real daily OHLC from data/ohlc (ignored if it is placeholder data)."""
    for f in sorted((ROOT / "data" / "ohlc").glob("*.csv")):
        try:
            d = pd.read_csv(f)
            d.columns = [c.strip().lower() for c in d.columns]
            dc = next(c for c in ("day", "date", "timestamp") if c in d.columns)
            d["day"] = pd.to_datetime(d[dc], errors="coerce")
            d = d.dropna(subset=["day"])
            if d["close"].nunique() > 50:
                return d[["day", "open", "high", "low", "close"]].sort_values("day").reset_index(drop=True)
        except Exception:
            continue
    return None


def best_daily(intraday: pd.DataFrame) -> pd.DataFrame:
    """Daily file + intraday-derived days (intraday wins for recent dates)."""
    a = daily_from_intraday(intraday)
    f = load_daily_file()
    if f is None:
        return a
    f = f[~f["day"].dt.normalize().isin(pd.to_datetime(a["day"]).dt.normalize())]
    return pd.concat([f, a], ignore_index=True).sort_values("day").reset_index(drop=True)


def bias_validation() -> dict | None:
    try:
        return json.loads(BT_PATH.read_text())
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════
# Zones
# ═══════════════════════════════════════════════════════════════════════════
def inverted_fvgs(df: pd.DataFrame) -> list[tuple[str, float, float, int]]:
    """FVG that price CLOSED through → flips polarity. (direction_after_flip, lo, hi, i_flip)."""
    out = []
    c = df["close"].to_numpy(float)
    for g in smc.FVG_Tracker(df).fvgs:
        lo, hi = g.bottom, g.top
        for j in range(g.i + 1, len(df)):
            if g.direction == "BEARISH" and c[j] > hi:
                out.append(("BULLISH", lo, hi, j)); break
            if g.direction == "BULLISH" and c[j] < lo:
                out.append(("BEARISH", lo, hi, j)); break
    return out


def zones(df: pd.DataFrame, side: str, n: int = 2) -> list[tuple[float, float, int, str]]:
    r = smc.read(df, n=n)
    z = []
    for ob in r.get("open_obs", []):
        if ob.direction == side:
            z.append((ob.low, ob.high, ob.i, "OB"))
    for g in r.get("open_fvgs", []):
        if g.direction == side:
            z.append((g.bottom, g.top, g.i, "FVG"))
    for d, lo, hi, j in inverted_fvgs(df):
        if d == side:
            z.append((lo, hi, j, "IFVG"))
    return z


# ═══════════════════════════════════════════════════════════════════════════
# Evaluate
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class FiveRule:
    side: str = "NONE"                      # BULLISH | BEARISH | NONE
    rules: list = field(default_factory=list)   # (name, ok, detail)
    verdict: str = "NO TRADE"
    next_needed: str = ""
    entry: tuple | None = None
    sl: float | None = None
    tp: float | None = None
    tp_name: str = ""
    rr: float | None = None
    daily: tuple = ("NONE", "")
    daily_validated: bool | None = None
    external: External | None = None
    levels: dict = field(default_factory=dict)

    @property
    def score(self) -> int:
        return sum(1 for _, ok, _ in self.rules if ok)


def _session(df: pd.DataFrame, day) -> pd.DataFrame:
    t = _ts(df)
    return df[t.dt.date == day].reset_index(drop=True)


def evaluate(df1: pd.DataFrame | None, df5: pd.DataFrame, df15: pd.DataFrame | None,
             now: datetime | None = None, daily: pd.DataFrame | None = None, rr: dict | None = None) -> FiveRule:
    rr = rr or _rules()
    out = FiveRule()
    t5 = _ts(df5)
    now = now or t5.iloc[-1].to_pydatetime()
    today = t5.iloc[-1].date()
    spot = float(df5["close"].iloc[-1])
    df15 = df15 if df15 is not None and len(df15) >= 15 else (
        df5.set_index(t5).resample("15min", origin="start_day", offset="15min")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna().reset_index()
        .rename(columns={"index": "timestamp"}))
    if "timestamp" not in df15:
        df15 = df15.reset_index().rename(columns={df15.index.name or "index": "timestamp"})

    # RULE 1 — bias
    ext = external_structure(df15, int(rr["min_pullback_bars"]))
    out.external = ext
    daily = daily if daily is not None else daily_from_intraday(df5)
    out.daily = daily_bias(daily, today)
    val = bias_validation()
    oc = (val or {}).get("outcomes", {})
    out.daily_validated = bool(oc.get("dir", {}).get("passed")) if val else None       # DIRECTION use
    draw_ok = bool(oc.get("draw", {}).get("passed")) if val else False                # TARGET use
    out.levels["draw_validated"] = draw_ok
    draw_note = ""
    if draw_ok and out.daily[0] in ("BULLISH", "BEARISH"):
        prev_d = daily[pd.to_datetime(daily["day"]).dt.date < today].tail(1)
        bt_ = oc["draw"]["by_type"].get("BULL" if out.daily[0] == "BULLISH" else "BEAR", {})
        side_edge = (bt_.get("acc", 0) - bt_.get("chance", 1)) * 100
        side_ok = side_edge >= float(rr["draw_min_edge_pts"]) and bt_.get("p", 1) <= float(rr["draw_max_p"])
        if not side_ok:
            draw_note = (f"; daily draw for {out.daily[0].lower()} reads is too weak on your NIFTY data "
                         f"({side_edge:+.1f} pts, p={bt_.get('p', 1):.3f}) — not used")
        if len(prev_d) and side_ok:
            out.levels["daily_draw"] = (float(prev_d.high.iloc[0]) if out.daily[0] == "BULLISH" else float(prev_d.low.iloc[0]),
                                        "PDH" if out.daily[0] == "BULLISH" else "PDL", bt_.get("acc"), bt_.get("chance"))
    side = ext.trend if ext.trend in ("BULLISH", "BEARISH") else "NONE"
    dtxt = f"daily candle says {out.daily[0].lower()} ({out.daily[1]})"
    if out.daily_validated and out.daily[0] in ("BULLISH", "BEARISH") and side != "NONE" and out.daily[0] != side:
        ok1, det1 = False, f"15m external {side.lower()} but validated daily bias is {out.daily[0].lower()} — conflict, stand aside"
        side_eval = side
    else:
        ok1 = side != "NONE"
        tag = ("direction validated ✔" if out.daily_validated else
               "info only — not validated on NIFTY yet" if out.daily_validated is None else
               "not used for direction (failed the NIFTY direction test)")
        tag += draw_note
        dd = out.levels.get("daily_draw")
        if dd:
            tod = df5[_ts(df5).dt.date == today]
            hit = bool(len(tod)) and ((tod["high"].max() > dd[0]) if dd[1] == "PDH" else (tod["low"].min() < dd[0]))
            tag += (f"; daily draw {dd[1]} {dd[0]:,.0f} " +
                    ("already TAKEN today — target reached" if hit else
                     f"likely to be taken today ({dd[2] * 100:.0f}% on your NIFTY data vs {dd[3] * 100:.0f}% chance)"))
            if hit:
                out.levels.pop("daily_draw", None)
        det1 = (f"15m external structure {side.lower()} (swing {ext.swing_low:,.0f}–{ext.swing_high:,.0f}); "
                f"{dtxt} [{tag}]") if ok1 else f"15m external structure unclear; {dtxt}"
        side_eval = side
    out.side = side_eval
    out.rules.append(("1 · Bias", ok1, det1))
    sgn = 1 if side_eval == "BULLISH" else -1

    # RULE 2 — time window
    tw = [(dtime(*map(int, a.split(":"))), dtime(*map(int, b.split(":")))) for a, b in rr["windows"]]
    ok2 = any(a <= now.time() <= b for a, b in tw)
    out.rules.append(("2 · Time window", ok2, f"{now:%H:%M} · windows " + ", ".join(f"{a:%H:%M}–{b:%H:%M}" for a, b in tw)))

    if side_eval == "NONE":
        for nm in ("3 · Liquidation", "4 · LTF confirmation", "5 · Entry & R:R"):
            out.rules.append((nm, False, "needs a bias first"))
        out.next_needed = "Rule 1 — wait for clear 15m external structure"
        return out

    # RULE 3 — liquidation today
    s5 = _session(df5, today)
    ts5 = _ts(s5)
    lv = {}
    orm = int(rr["opening_range_minutes"])
    or_mask = ts5 < ts5.iloc[0].normalize() + pd.Timedelta(hours=9, minutes=15 + orm)
    if or_mask.any():
        lv["OR high"], lv["OR low"] = float(s5.loc[or_mask, "high"].max()), float(s5.loc[or_mask, "low"].min())
    ib_mask = ts5 < ts5.iloc[0].normalize() + pd.Timedelta(hours=9, minutes=15 + int(rr["ib_minutes"]))
    if ib_mask.any() and (~ib_mask).any():
        lv["IB high"], lv["IB low"] = float(s5.loc[ib_mask, "high"].max()), float(s5.loc[ib_mask, "low"].min())
    prev = daily[pd.to_datetime(daily["day"]).dt.date < today].tail(1)
    if len(prev):
        lv["PDH"], lv["PDL"] = float(prev.high.iloc[0]), float(prev.low.iloc[0])
    out.levels = lv
    swept = []
    after = s5[~or_mask] if or_mask.any() else s5
    lows, highs = after["low"].to_numpy(float), after["high"].to_numpy(float)
    for name in (("OR low", "IB low", "PDL") if sgn > 0 else ("OR high", "IB high", "PDH")):
        if name not in lv:
            continue
        x = lv[name]
        hit = (lows < x).any() if sgn > 0 else (highs > x).any()
        reclaimed = spot > x if sgn > 0 else spot < x
        if hit and reclaimed:
            swept.append((name, x))
    t15 = _ts(df15)
    for i, kind, lvl in ext.sweeps:
        if (kind == "LOW") == (sgn > 0) and i < len(t15) and t15.iloc[i].date() == today:
            swept.append(("15m swing " + ("low" if sgn > 0 else "high"), lvl))
    ok3 = bool(swept)
    det3 = (("Swept & reclaimed: " + ", ".join(f"{n} {x:,.0f}" for n, x in swept)) if ok3 else
            "No " + ("sell-side" if sgn > 0 else "buy-side") + " level swept yet today (" +
            ", ".join(f"{k} {v:,.0f}" for k, v in lv.items() if (("low" in k or k == "PDL") if sgn > 0 else ("high" in k or k == "PDH"))) + ")")
    out.rules.append(("3 · Liquidation", ok3, det3))

    # RULE 4 — zone tap + 1m CHoCH
    zside = side_eval
    z = zones(df15, zside) + zones(df5.tail(150).reset_index(drop=True), zside)
    tap_from = t5.iloc[-1] - pd.Timedelta(minutes=int(rr["zone_tap_lookback_min"]))
    ltf = df1 if df1 is not None and len(df1) >= 30 else None
    ltf_name = "1m" if ltf is not None else "5m (1m unavailable)"
    ltf = (ltf if ltf is not None else df5).tail(int(rr["ltf_lookback_bars"])).reset_index(drop=True)
    tl = _ts(ltf)
    recent = ltf[tl >= tap_from]
    tapped, tap_last = None, None
    for lo, hi, i, kind in z:
        if not len(recent):
            break
        m = (recent["low"] <= hi) & (recent["high"] >= lo)
        if m.any():
            lt = _ts(recent)[m].iloc[-1]
            if tap_last is None or lt > tap_last:
                tapped, tap_last = (lo, hi, kind), lt
    shift = None
    try:
        st = smc.read(ltf, n=2)["structure"]
        _m = (ltf["low"] <= tapped[1]) & (ltf["high"] >= tapped[0]) & (tl >= tap_from) if tapped else None
        tap_t = tl[_m].iloc[0] if tapped and _m.any() else None
        for e in reversed(st.events):
            if e.direction == zside and (e.is_mss or e.kind in ("CHOCH", "MSS")):
                if tap_t is None or pd.Timestamp(e.at) >= tap_t:
                    shift = e
                break
    except Exception:
        pass
    ok4 = bool(tapped and shift)
    det4 = (f"Tapped {zside.lower()} {tapped[2]} {tapped[0]:,.0f}–{tapped[1]:,.0f}, then {ltf_name} CHoCH "
            f"{'up' if sgn > 0 else 'down'} at {pd.Timestamp(shift.at):%H:%M} ({shift.level:,.0f})" if ok4 else
            (f"Zone tapped ({tapped[2]} {tapped[0]:,.0f}–{tapped[1]:,.0f}) — waiting for {ltf_name} CHoCH "
             f"{'up' if sgn > 0 else 'down'}" if tapped else
             f"Price has not tapped a {zside.lower()} zone in the last {rr['zone_tap_lookback_min']} min"))
    out.rules.append(("4 · LTF confirmation", ok4, det4))

    # RULE 5 — entry zone + R:R
    z5 = zones(df5.tail(150).reset_index(drop=True), zside)
    cand = [x for x in z5 if (x[1] <= spot + 3 if sgn > 0 else x[0] >= spot - 3)]
    cand = sorted(cand, key=lambda x: abs((x[0] + x[1]) / 2 - spot))
    ok5, det5 = False, "No 5m OB / FVG / IFVG on the right side of price"
    if cand:
        lo, hi, _, kind = cand[0]
        if hi - lo < 4:
            lo, hi = lo - 2, hi + 2
        buf = float(rr["sl_buffer_pts"])
        # SL beyond the zone AND the LTF swing that confirmed the turn (since the tap) — not the day's extreme
        conf = ltf[tl >= tap_from] if len(ltf) else ltf
        conf_ext = (float(conf["low"].min()) if sgn > 0 else float(conf["high"].max())) if len(conf) else None
        if conf_ext is not None and abs(conf_ext - (lo if sgn > 0 else hi)) > 60:
            conf_ext = None                                    # too far — ignore
        sl = (min(lo, conf_ext if conf_ext else lo) - buf) if sgn > 0 else (max(hi, conf_ext if conf_ext else hi) + buf)
        mid = (lo + hi) / 2
        msl = float(rr["min_sl_pts"])
        if abs(mid - sl) < msl:                                # NIFTY 5m noise — never tighter than this
            sl = mid - msl if sgn > 0 else mid + msl
        tp = ext.swing_high if sgn > 0 else ext.swing_low
        # nearer TP1: closest 5m swing (internal liquidity) between entry and the external target
        try:
            piv = smc.read(df5.tail(150).reset_index(drop=True), n=2)["pivots"]
            near = [p.price for p in piv if (p.kind == "HIGH" and sgn > 0 and hi < p.price < (tp or 1e9))
                    or (p.kind == "LOW" and sgn < 0 and (tp or -1e9) < p.price < lo)]
            out.levels["TP1"] = (min(near) if sgn > 0 else max(near)) if near else None
            dd = out.levels.get("daily_draw")
            if dd and ((sgn > 0 and dd[1] == "PDH" and dd[0] > hi) or (sgn < 0 and dd[1] == "PDL" and dd[0] < lo)):
                out.levels["TP1"] = dd[0]                    # validated daily draw beats a random internal swing
                out.levels["TP1_name"] = f"{dd[1]} (validated daily draw)"
        except Exception:
            out.levels["TP1"] = None
        out.entry, out.sl, out.tp, out.tp_name = (lo, hi), sl, tp, f"15m external swing {'high' if sgn > 0 else 'low'}"
        if tp is not None and ((tp > hi) if sgn > 0 else (tp < lo)):
            out.rr = abs(tp - mid) / abs(mid - sl) if mid != sl else None
            ok5 = (out.rr or 0) >= float(rr["min_rr"])
            det5 = (f"5m {kind} {lo:,.0f}–{hi:,.0f} · SL {sl:,.0f} · TP {tp:,.0f} ({out.tp_name}) · "
                    f"R:R {out.rr:.1f} {'≥' if ok5 else '<'} {rr['min_rr']}")
        else:
            det5 = f"5m {kind} {lo:,.0f}–{hi:,.0f} but external target {tp:,.0f} is not beyond the zone — no room"
    out.rules.append(("5 · Entry & R:R", ok5, det5))

    if all(ok for _, ok, _ in out.rules):
        out.verdict = f"TAKE TRADE · {'BUY' if sgn > 0 else 'SELL'}"
    elif all(ok for nm, ok, _ in out.rules if not nm.startswith("2")) and not ok2:
        out.verdict = "SETUP VALID · OUTSIDE TIME WINDOW"
    else:
        out.verdict = "NO TRADE"
        first = next(nm for nm, ok, _ in out.rules if not ok)
        out.next_needed = first
        if out.score >= 3:
            out.verdict = f"GET READY · {out.score}/5"
    return out


def text_block(fr: FiveRule) -> str:
    L = [f"5-RULE SMC MODEL · {fr.side.lower()} side · {fr.score}/5 → {fr.verdict}"]
    for nm, ok, det in fr.rules:
        L.append(f"  {'✔' if ok else '✘'} {nm}: {det}")
    if fr.entry and fr.sl and fr.tp:
        tp1 = fr.levels.get("TP1")
        L.append(f"  Plan: entry {fr.entry[0]:,.0f}–{fr.entry[1]:,.0f} · SL {fr.sl:,.0f}"
                 + (f" · TP1 {tp1:,.0f} ({fr.levels.get('TP1_name', 'nearest swing')})" if tp1 else "")
                 + f" · TP2 {fr.tp:,.0f} (external)" + (f" · R:R {fr.rr:.1f} to TP2" if fr.rr else ""))
    if fr.next_needed and "TAKE" not in fr.verdict:
        L.append(f"  Next needed: {fr.next_needed}")
    return "\n".join(L)
