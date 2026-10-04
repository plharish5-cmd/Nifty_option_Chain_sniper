"""
candle_patterns.py — classic candlestick patterns on the 15m / 30m / 1h NIFTY bars (built from stored 1m by htf_candles).

Patterns (all rules are numbers in candle_rules.json, all causal — a pattern is known only when its last bar has closed):
  single   Hammer · Inverted hammer · Hanging man · Shooting star · Pin bar (bull/bear) · Doji · Spinning top · Marubozu (bull/bear)
  two-bar  Bullish/Bearish Engulfing · Bullish/Bearish Harami · Piercing line · Dark cloud cover · Tweezer bottom/top
  three    Morning star · Evening star · Three white soldiers · Three black crows
Reversal patterns need a prior move into them (hammer after a fall, shooting star after a rise …) — "trend_bars" bars before
the pattern must have moved at least "trend_atr" x ATR. Doji / spinning top are indecision: tested as "reverses the prior move".

Whether any of them predicts the next move is NOT assumed — backtest_candles.py measures it against random same-time bars,
and the briefing line shows each pattern together with that verdict.

  .venv/bin/python candle_patterns.py                 # patterns on the last closed bars today (15m/30m/1h)
  .venv/bin/python candle_patterns.py --day 2026-10-01
Information only — not financial advice.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
BT = ROOT / "data" / "backtests"


def rules() -> dict:
    return json.loads((ROOT / "candle_rules.json").read_text())


# name → (direction the textbook claims: +1 up, -1 down, 0 = prior-move reversal, "bars" the pattern spans, plain-English meaning)
PATTERNS = {
    "Hammer":                 (+1, 1, "after a fall, long lower wick → buyers rejected lower prices"),
    "Inverted hammer":        (+1, 1, "after a fall, long upper wick → first buying attempt"),
    "Hanging man":            (-1, 1, "after a rise, hammer shape → warning the rise is tiring"),
    "Shooting star":          (-1, 1, "after a rise, long upper wick → sellers rejected higher prices"),
    "Bullish pin bar":        (+1, 1, "wick pokes below the last low and closes back above it"),
    "Bearish pin bar":        (-1, 1, "wick pokes above the last high and closes back below it"),
    "Doji":                   (0, 1, "open ≈ close → indecision; may reverse the prior move"),
    "Spinning top":           (0, 1, "small body, wicks both sides → indecision; may reverse the prior move"),
    "Bullish marubozu":       (+1, 1, "full-body green bar, no wicks → buyers in control (continuation)"),
    "Bearish marubozu":       (-1, 1, "full-body red bar, no wicks → sellers in control (continuation)"),
    "Bullish engulfing":      (+1, 2, "after a fall, green body swallows the red body"),
    "Bearish engulfing":      (-1, 2, "after a rise, red body swallows the green body"),
    "Bullish harami":         (+1, 2, "after a fall, small green body inside the big red body"),
    "Bearish harami":         (-1, 2, "after a rise, small red body inside the big green body"),
    "Piercing line":          (+1, 2, "after a fall, green bar closes above the middle of the red bar"),
    "Dark cloud cover":       (-1, 2, "after a rise, red bar closes below the middle of the green bar"),
    "Tweezer bottom":         (+1, 2, "after a fall, two bars with the same low"),
    "Tweezer top":            (-1, 2, "after a rise, two bars with the same high"),
    "Morning star":           (+1, 3, "after a fall: big red, small star, big green"),
    "Evening star":           (-1, 3, "after a rise: big green, small star, big red"),
    "Three white soldiers":   (+1, 3, "after a fall: three rising green bars"),
    "Three black crows":      (-1, 3, "after a rise: three falling red bars"),
    "Long lower wick":        (+1, 1, "lower wick is half the bar or more → buyers rejected the low"),
    "Long upper wick":        (-1, 1, "upper wick is half the bar or more → sellers rejected the high"),
}
ARROW = {1: "↑", -1: "↓", 0: "~"}


def _atr(h, l, c, n):
    pc = np.r_[c[0], c[:-1]]
    tr = np.maximum.reduce([h - l, np.abs(h - pc), np.abs(l - pc)])
    return pd.Series(tr).rolling(n, min_periods=n).mean().to_numpy()


def _S(a: np.ndarray, k: int) -> np.ndarray:
    """a shifted by k bars (value k bars ago); NaN where unavailable."""
    out = np.full(len(a), np.nan)
    if k == 0:
        return a.astype(float).copy()
    if k < len(a):
        out[k:] = a[:-k]
    return out


def prepare(bars: pd.DataFrame, R: dict) -> dict:
    o, h, l, c = (bars[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    day = np.array([d.toordinal() for d in bars["timestamp"].dt.date], dtype=float)
    atr = _atr(h, l, c, int(R["atr_n"]))
    return dict(o=o, h=h, l=l, c=c, day=day, atr=atr)


def detect(bars: pd.DataFrame, R: dict | None = None) -> dict:
    """Boolean array per pattern name (True on the bar where the pattern COMPLETES) + 'drift' (prior move in ATR, same index)."""
    R = R or rules()
    A = prepare(bars, R)
    o, h, l, c, day, atr = A["o"], A["h"], A["l"], A["c"], A["day"], A["atr"]
    tol = float(R["tol_atr"]) * atr
    rng = h - l
    body = np.abs(c - o)
    top, bot = np.maximum(o, c), np.minimum(o, c)
    upw, low = h - top, bot - l
    bull, bear = c > o, c < o
    with np.errstate(invalid="ignore", divide="ignore"):
        ok = (rng >= float(R["min_range_atr"]) * atr) & (rng > 0)
        bf, uf, lf = body / rng, upw / rng, low / rng

        def ss(k):                                                   # whole pattern inside one session
            return _S(day, k) == day

        def drift(m):                                                # prior move (ATR) into a pattern that starts m bars back
            lb = int(R["trend_bars"])
            return (_S(c, m + 1) - _S(c, m + 1 + lb)) / _S(atr, m + 1)

        ta = float(R["trend_atr"])
        down = lambda m: drift(m) <= -ta                             # noqa: E731
        up = lambda m: drift(m) >= ta                                # noqa: E731
        big = float(R["big_body_atr"]) * atr
        o1, c1, h1, l1, b1, bu1, be1 = (_S(x, 1) for x in (o, c, h, l, body, bull, bear))
        top1, bot1 = _S(top, 1), _S(bot, 1)
        bull1, bear1 = bu1 > 0, be1 > 0
        mid1 = (o1 + c1) / 2
        o2, c2, b2 = _S(o, 2), _S(c, 2), _S(body, 2)
        bull2, bear2 = _S(bull, 2) > 0, _S(bear, 2) > 0
        top2, bot2 = _S(top, 2), _S(bot, 2)

        sm, sh_, ob, bm = float(R["shadow_min"]), float(R["opp_wick_max"]), float(R["body_min"]), float(R["pin_body_max"])
        hammer_shape = ok & (lf >= sm) & (uf <= sh_) & (bf >= ob)
        inv_shape = ok & (uf >= sm) & (lf <= sh_) & (bf >= ob)
        P: dict[str, np.ndarray] = {}
        P["Hammer"] = hammer_shape & down(0)
        P["Inverted hammer"] = inv_shape & down(0)
        P["Hanging man"] = hammer_shape & up(0)
        P["Shooting star"] = inv_shape & up(0)
        P["Bullish pin bar"] = ok & (lf >= sm) & (bf <= bm) & (l < l1) & (c > l1)
        P["Bearish pin bar"] = ok & (uf >= sm) & (bf <= bm) & (h > h1) & (c < h1)
        P["Doji"] = ok & (bf <= float(R["doji_body"]))
        sw = float(R["spin_wick"])
        P["Spinning top"] = ok & (bf > float(R["doji_body"])) & (bf <= float(R["spin_body"])) & (uf >= sw) & (lf >= sw)
        mb, mr = float(R["maru_body"]), float(R["maru_range_atr"]) * atr
        P["Bullish marubozu"] = (rng >= mr) & (bf >= mb) & bull
        P["Bearish marubozu"] = (rng >= mr) & (bf >= mb) & bear

        two = ss(1) & ok
        P["Bullish engulfing"] = two & bear1 & bull & (b1 >= 0.2 * atr) & (body >= b1) & (o <= c1 + tol) & (c >= o1) & down(1)
        P["Bearish engulfing"] = two & bull1 & bear & (b1 >= 0.2 * atr) & (body >= b1) & (o >= c1 - tol) & (c <= o1) & up(1)
        hf = float(R["harami_frac"])
        P["Bullish harami"] = two & bear1 & (b1 >= big) & bull & (body <= hf * b1) & (top <= o1) & (bot >= c1) & down(1)
        P["Bearish harami"] = two & bull1 & (b1 >= big) & bear & (body <= hf * b1) & (top <= c1) & (bot >= o1) & up(1)
        P["Piercing line"] = two & bear1 & (b1 >= big) & bull & (o <= c1 + tol) & (c > mid1) & (c < o1) & down(1)
        P["Dark cloud cover"] = two & bull1 & (b1 >= big) & bear & (o >= c1 - tol) & (c < mid1) & (c > o1) & up(1)
        P["Tweezer bottom"] = two & bear1 & bull & (np.abs(l - l1) <= tol) & down(1)
        P["Tweezer top"] = two & bull1 & bear & (np.abs(h - h1) <= tol) & up(1)

        three = ss(2) & ok
        sf = float(R["star_body_frac"])
        star_ok = _S(body, 1) <= sf * b2
        P["Morning star"] = three & bear2 & (b2 >= _S(big, 2)) & star_ok & (top1 <= c2 + tol) & bull & (c > (o2 + c2) / 2) & down(2)
        P["Evening star"] = three & bull2 & (b2 >= _S(big, 2)) & star_ok & (bot1 >= c2 - tol) & bear & (c < (o2 + c2) / 2) & up(2)
        sb, sw_ = float(R["soldier_body"]), float(R["soldier_wick_max"])
        solid_u = lambda r_b, r_w: (r_b >= sb) & (r_w <= sw_)        # noqa: E731
        bf1, bf2 = _S(bf, 1), _S(bf, 2)
        uf1, uf2 = _S(uf, 1), _S(uf, 2)
        lf1, lf2 = _S(lf, 1), _S(lf, 2)
        P["Three white soldiers"] = (three & bull & bull1 & bull2 & (c > c1) & (c1 > c2) & (o >= o1 - tol) & (o <= c1 + tol) & (o1 >= o2 - tol) & (o1 <= c2 + tol)
                                     & solid_u(bf, uf) & solid_u(bf1, uf1) & solid_u(bf2, uf2) & down(2))
        P["Three black crows"] = (three & bear & bear1 & bear2 & (c < c1) & (c1 < c2) & (o <= o1 + tol) & (o >= c1 - tol) & (o1 <= o2 + tol) & (o1 >= c2 - tol)
                                  & solid_u(bf, lf) & solid_u(bf1, lf1) & solid_u(bf2, lf2) & up(2))
        W = R["wick"]
        okw = (rng >= float(W["min_range_atr"]) * atr) & (rng > 0)
        P["Long lower wick"] = okw & (lf >= float(W["long"]))
        P["Long upper wick"] = okw & (uf >= float(W["long"]))
        d0 = drift(0)
    out = {k: np.asarray(v, dtype=bool) & np.isfinite(atr) for k, v in P.items()}
    out["_drift"] = d0
    return out


def direction(name: str, drift_v: float) -> int:
    """+1 / -1 expected direction; neutral patterns (doji, spinning top) are tested as a reversal of the prior move."""
    d = PATTERNS[name][0]
    if d != 0:
        return d
    return 0 if not np.isfinite(drift_v) or drift_v == 0 else (-1 if drift_v > 0 else 1)


def level_table(bars: pd.DataFrame, m1: pd.DataFrame, R: dict | None = None) -> dict:
    """Known-in-advance levels per bar: previous-day high/low, opening range (first 15 min) high/low, earlier-bar day high/low.
    Returns near_lo / near_hi (bar low / high within level_tol_atr x ATR of ANY level) plus the nearest level's name and distance."""
    R = R or rules()
    n = len(bars)
    dates = bars["timestamp"].dt.date
    h, l = bars["high"].to_numpy(float), bars["low"].to_numpy(float)
    atr = _atr(h, l, bars["close"].to_numpy(float), int(R["atr_n"]))
    daily = bars.groupby(dates).agg(H=("high", "max"), L=("low", "min"))
    prev = daily.shift(1)
    pdh, pdl = dates.map(prev["H"]).to_numpy(float), dates.map(prev["L"]).to_numpy(float)
    mt = m1["timestamp"]
    first = m1[(mt.dt.hour * 60 + mt.dt.minute) < 9 * 60 + 30]
    orr = first.groupby(first["timestamp"].dt.date).agg(H=("high", "max"), L=("low", "min"))
    start_ok = ((bars["timestamp"].dt.hour * 60 + bars["timestamp"].dt.minute) >= 9 * 60 + 30).to_numpy()
    orh = np.where(start_ok, dates.map(orr["H"]).to_numpy(float), np.nan)
    orl = np.where(start_ok, dates.map(orr["L"]).to_numpy(float), np.nan)
    dh = bars.groupby(dates)["high"].transform(lambda x: x.cummax().shift(1)).to_numpy(float)
    dl = bars.groupby(dates)["low"].transform(lambda x: x.cummin().shift(1)).to_numpy(float)
    names = ["prev-day high", "prev-day low", "opening-range high", "opening-range low", "day high so far", "day low so far"]
    L = np.vstack([pdh, pdl, orh, orl, dh, dl])
    tol = float(R["wick"]["level_tol_atr"]) * atr

    def nearest(px):
        with np.errstate(invalid="ignore"):
            dist = np.abs(L - px[None, :])
        dist = np.where(np.isfinite(dist), dist, np.inf)
        k = dist.argmin(axis=0)
        d = dist[k, np.arange(n)]
        return k, d
    kl, dl_ = nearest(l)
    kh, dh_ = nearest(h)
    return dict(near_lo=(dl_ <= tol) & np.isfinite(atr), near_hi=(dh_ <= tol) & np.isfinite(atr),
                lo_name=[names[k] for k in kl], lo_dist=dl_, hi_name=[names[k] for k in kh], hi_dist=dh_, levels=L, names=names)


def forming_bar(m1: pd.DataFrame, tf: int, now: datetime) -> dict | None:
    """The bar that is open right now (partial, from 1-minute candles)."""
    day0 = pd.Timestamp(now).normalize()
    t0 = day0 + pd.Timedelta(hours=9, minutes=15)
    t_end = day0 + pd.Timedelta(hours=15, minutes=30)
    nowt = pd.Timestamp(now)
    if nowt < t0 or nowt >= t_end:
        return None
    k = int((nowt - t0) / pd.Timedelta(minutes=tf))
    start = t0 + pd.Timedelta(minutes=tf * k)
    expected = int(min(tf, (t_end - start) / pd.Timedelta(minutes=1)))
    if expected <= 0.5 * tf:
        return None
    g = m1[(m1["timestamp"] >= start) & (m1["timestamp"] < nowt)]
    if g.empty:
        return None
    return dict(timestamp=start, ct=start + pd.Timedelta(minutes=expected), open=float(g["open"].iloc[0]), high=float(g["high"].max()),
                low=float(g["low"].min()), close=float(g["close"].iloc[-1]), minutes=len(g), expected=expected)


def wick_read(o, h, l, c) -> dict:
    rng = max(h - l, 1e-9)
    top, bot = max(o, c), min(o, c)
    return dict(rng=h - l, body=abs(c - o) / rng, up=(h - top) / rng, lo=(bot - l) / rng, color="green" if c > o else ("red" if c < o else "flat"))


# ═══════════════════════════════════════════════════════════════════════════
# Briefing
# ═══════════════════════════════════════════════════════════════════════════
def verdicts() -> dict:
    try:
        return json.loads((BT / "candles.json").read_text()).get("patterns", {})
    except Exception:
        return {}


def _tag(name: str, V: dict, at_level: bool = False) -> str:
    v = V.get(name)
    if at_level and v and v.get("lvl"):
        v = dict(v["lvl"], n=v["lvl"].get("n", 0))
        where = "at a level: "
    else:
        where = ""
    if not v or "verdict" not in v:
        return "not tested yet"
    if v["verdict"] == "EDGE":
        return f"✔ {where}tested edge ({v['hit']*100:.0f}% vs {v['ctrl_hit']*100:.0f}% control, n={v['n']})"
    if v["verdict"] == "INVERSE":
        return f"⚠ {where}tested: moves OPPOSITE to textbook (n={v['n']})"
    if v["verdict"] in ("LEAN", "LEAN_INV"):
        way = "textbook way" if v["verdict"] == "LEAN" else "OPPOSITE to textbook"
        return f"◐ {where}weak lead only, not proven ({way}; {v['hit']*100:.0f}% vs {v['ctrl_hit']*100:.0f}% control, n={v['n']})"
    if v["verdict"] == "FEW":
        return f"{where}too few samples (n={v['n']})"
    return f"✘ {where}no edge ({v['hit']*100:.0f}% vs {v['ctrl_hit']*100:.0f}% control, n={v['n']})"


def _tfname(tf: int) -> str:
    return f"{tf}m" if tf < 60 else f"{tf // 60}h"


def _px(x: float) -> str:
    return f"{x:,.0f}"


def _wick_text(w: dict, L: dict | None, i: int, Rw: dict, o, h, l, c) -> str:
    """One-line plain read of a bar: colour, body/wick shares, long-wick flag, and any level the wick touches."""
    lng = float(Rw["long"])
    bits = [f"{'GREEN' if w['color'] == 'green' else ('RED' if w['color'] == 'red' else 'flat')} O {_px(o)} H {_px(h)} L {_px(l)} C {_px(c)}",
            f"body {w['body']*100:.0f}% · upper wick {w['up']*100:.0f}% · lower wick {w['lo']*100:.0f}%"]
    flags = []
    if w["lo"] >= lng:
        s_ = "◀ LONG LOWER WICK (buyers rejected lower prices)"
        if L is not None and bool(L["near_lo"][i]):
            s_ += f" — low {_px(l)} is at {L['lo_name'][i]} ({L['lo_dist'][i]:.0f} pts away)"
        flags.append(s_)
    if w["up"] >= lng:
        s_ = "◀ LONG UPPER WICK (sellers rejected higher prices)"
        if L is not None and bool(L["near_hi"][i]):
            s_ += f" — high {_px(h)} is at {L['hi_name'][i]} ({L['hi_dist'][i]:.0f} pts away)"
        flags.append(s_)
    return " · ".join(bits) + ("  " + "  ".join(flags) if flags else "")


def latest(m1: pd.DataFrame, now: datetime | None = None, R: dict | None = None) -> list[dict]:
    """Per briefing timeframe: the last CLOSED bar and the FORMING bar (as if it closed now) with patterns, wicks and levels."""
    import htf_candles as hc
    R = R or rules()
    B = R["brief"]
    now = now or datetime.now()
    rows = []
    for tf in B["timeframes"]:
        bars = hc.htf(m1, int(tf))
        if bars.empty:
            continue
        bars = bars[bars["ct"] <= pd.Timestamp(now)].reset_index(drop=True)
        need = int(R["atr_n"]) + int(R["trend_bars"]) + 3
        if len(bars) < need:
            rows.append(dict(tf=tf, closed=None, note="not enough closed bars yet"))
            continue
        row = dict(tf=tf, note="")
        for kind in ("closed", "forming"):
            if kind == "closed":
                bb = bars
            else:
                fb = forming_bar(m1, int(tf), now)
                if fb is None or fb["minutes"] < float(B["forming_min_frac"]) * fb["expected"]:
                    row["forming"] = dict(just_opened=fb is not None, fb=fb)
                    continue
                bb = pd.concat([bars, pd.DataFrame([{k: fb[k] for k in ("timestamp", "ct", "open", "high", "low", "close")}])], ignore_index=True)
            D = detect(bb, R)
            L = level_table(bb, m1, R)
            i = len(bb) - 1
            o, h, l, c = (float(bb[k].iloc[i]) for k in ("open", "high", "low", "close"))
            row[kind] = dict(ct=bb["ct"].iloc[i], start=bb["timestamp"].iloc[i], found=[n for n in PATTERNS if D[n][i]], drift=float(D["_drift"][i]) if np.isfinite(D["_drift"][i]) else 0.0,
                             w=wick_read(o, h, l, c), ohlc=(o, h, l, c), L=L, i=i, at_lo=bool(L["near_lo"][i]), at_hi=bool(L["near_hi"][i]),
                             fb=fb if kind == "forming" else None)
        rows.append(row)
    return rows


def _pattern_text(name: str, drift: float, V: dict, at_lo: bool, at_hi: bool) -> str:
    d = direction(name, drift)
    at_level = (d > 0 and at_lo) or (d < 0 and at_hi)
    note = ""
    if PATTERNS[name][0] == 0:
        note = " [after a rise → reversal ↓ expected]" if d < 0 else " [after a fall → reversal ↑ expected]"
    return f"{name} (textbook {ARROW[d]}){note}{' · AT A LEVEL' if at_level else ''} · {_tag(name, V, at_level)}"


def brief_lines(m1: pd.DataFrame | None, now: datetime | None = None) -> list[str]:
    R = rules()
    if m1 is None or len(m1) == 0:
        return ["CANDLES: N/A — no stored 1-minute candles."]
    now = now or datetime.now()
    V = verdicts()
    Rw = R["wick"]
    rows = latest(m1, now, R)
    n_edge = sum(1 for v in V.values() if v.get("verdict") == "EDGE") + sum(1 for v in V.values() if (v.get("lvl") or {}).get("verdict") == "EDGE")
    head = ("CANDLES · long wicks and patterns, 15m / 30m / 1h: " +
            ("no pattern has a proven edge on your NIFTY data — this is what the candle IS, not a signal." if V and n_edge == 0 else
             (f"{n_edge} pattern test(s) passed — see tags." if V else "backtest not run yet — names only, not signals.")))
    out = [head]
    for r in rows:
        tf = _tfname(int(r["tf"]))
        if r.get("closed") is None:
            out.append(f"  {tf:<3} {r.get('note', '')}")
            continue
        c = r["closed"]
        out.append(f"  {tf:<3} CLOSED {c['start']:%H:%M}–{c['ct']:%H:%M}: " + _wick_text(c["w"], c["L"], c["i"], Rw, *c["ohlc"]))
        for n in c["found"]:
            if n.startswith("Long "):
                continue                                   # already shown as the wick flag
            out.append(f"       ▸ {_pattern_text(n, c['drift'], V, c['at_lo'], c['at_hi'])}")
        f = r.get("forming")
        if f is None:
            continue
        if "w" not in f:
            out.append(f"  {tf:<3} FORMING: " + ("just opened — too early to read." if f.get("just_opened") else "no open bar now."))
            continue
        fb = f["fb"]
        out.append(f"  {tf:<3} FORMING {f['start']:%H:%M} ({fb['minutes']}/{fb['expected']} min, not final): " + _wick_text(f["w"], f["L"], f["i"], Rw, *f["ohlc"]))
        for n in f["found"]:
            if n.startswith("Long "):
                continue
            out.append(f"       ▸ if it closed now: {_pattern_text(n, f['drift'], V, f['at_lo'], f['at_hi'])}")
    return out


# ═══════════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════
def main():
    import backtest_five_rule as fr
    import htf_candles as hc
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=None)
    a = ap.parse_args()
    R = rules()
    m1 = fr.load_1m()
    if m1.empty:
        sys.exit("No stored 1-minute candles.")
    day = pd.Timestamp(a.day).date() if a.day else m1["timestamp"].dt.date.max()
    m1d = m1[m1["timestamp"].dt.date <= day]
    V = verdicts()
    for tf in R["brief"]["timeframes"]:
        bars = hc.htf(m1d, int(tf))
        D = detect(bars, R)
        print(f"\n── {tf}m patterns on {day} ──")
        sel = bars.index[bars["timestamp"].dt.date == day]
        for i in sel:
            f = [n for n in PATTERNS if D[n][i]]
            if f:
                print(f"  {bars['timestamp'].iloc[i]:%H:%M}  " + " · ".join(f"{n} {ARROW[direction(n, D['_drift'][i])]} [{_tag(n, V)}]" for n in f))
    print("\nInformation only — not financial advice.")


if __name__ == "__main__":
    main()
