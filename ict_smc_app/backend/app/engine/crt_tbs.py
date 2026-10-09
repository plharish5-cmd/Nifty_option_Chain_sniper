"""
engine/crt_tbs.py — Setup D: CRT (Candle Range Theory) + TBS (Turtle Body Soup), AR's method, as taught in the
course PDFs. A SEPARATE engine: it never touches the ICT/SMC playbook A/B/C (tests/test_baseline_lock.py).

HTF — the bias
  CRT candle   a CLOSED candle whose body is bigger than its two wicks together (body ≥ 50 % of its range) and
               "thick" (range ≥ min_range_atr × ATR). Thick = more liquidity = A+ CRT candle (≥ aplus_range_atr).
               CRTH / CRTL = its high / low, 50 % = the midpoint.
  Keylevel     on the SAME timeframe, found AFTER the CRT candle (course: "only after we found the CRT candle we need
               to find the keylevel"), using only what was known when the CRT candle closed:
                 OHP  the CRT candle took out an older, untaken, confirmed swing high      → bearish
                 OLP  the CRT candle took out an older, untaken, confirmed swing low       → bullish
                 OB   the CRT candle traded into a supply / demand order block            → bearish / bullish
                 FVG  the CRT candle traded into an open bearish / bullish fair value gap  → bearish / bullish
                 RB   the CRT candle traded into a rejection block — the long-wick part of a swing candle
                      (upper wick of a swing high → bearish, lower wick of a swing low → bullish)
  Manipulation the NEXT HTF candle. We act only inside its window ("as soon as the 2nd candle got manipulated we
               need to switch to LTF"). INSIDE-BAR CRT: if that 2nd candle closed fully inside the CRT range it
               manipulated nothing, so the 3rd candle becomes the manipulation candle.

LTF — the entry (every candle CLOSED; nothing repaints)
  TBS          the first candle whose BODY closes beyond the CRT level: above CRTH for a sell, below CRTL for a buy.
               A wick poke is not a TBS. A+ when 2–6 candles closed inside the level just before it (validation page;
               reported, not required unless crt_tbs.require_aplus_tbs).
  Model #1     sell: the thick BUY candle at / after the TBS (body ≥ m1_min_body_ratio of range) — the last one before
               the break (it is the order block). Buy: the thick SELL candle.
  Entry        whichever entry model confirms FIRST (all on closed LTF candles, after the TBS):
                 Model #1  order block — close beyond the thick opposite-colour candle (below its low for a sell)
                 Model #2  fair value gap — a 3-candle gap in the trade direction forms (entry at its 3rd candle)
                 Model #3  break of structure — close beyond the last LTF swing low (sell) / high (buy) before TBS
               Entry price = that close. 3-candle model: "pros enter on the manipulation candle, others wait for it
               to close and enter on the next one" → the entry may come until the next HTF candle closes.
  Stop         the extreme high / low since the TBS, plus sl_buffer_pts ("don't get greedy, no tiny stop").
  TP-1         the CRT 50 %: book HALF there, then move the stop to entry (breakeven) — never before TP-1.
  TP-2         the opposite CRT extreme (CRTL for a sell, CRTH for a buy).

Course "timeframe alignment" → pairs in rules.json crt_tbs.pairs:
  1h → 1m  scalping   ·   4h → 5m  short-term   ·   1D → 1h  day trading
NSE 4h candles are 09:15–13:15 and 13:15–15:30 (session-aligned like every other timeframe here).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .. import config
from ..signals.playbook import Signal
from ..timeutil import TF_SECONDS, bucket_start, chart_time, hm, in_windows, session_bounds, to_ist
from .analyze import Analysis, analyze
from .primitives import Bars, pivots


def R() -> Dict[str, Any]:
    return config.rules().get("crt_tbs", {})


@dataclass
class CRTRange:
    pair: str
    htf: str
    ltf: str
    k_ts: int                       # CRT candle start
    k_end: int                      # CRT candle close time
    win_ts: int                     # manipulation candle (HTF bucket) start
    win_end: int                    # manipulation candle close time
    crth: float
    crtl: float
    mid: float
    body_ratio: float
    range_atr: float
    aplus: bool
    keylevels: List[Dict[str, Any]] = field(default_factory=list)   # [{kind, dir, price | top/bottom, text}]
    biases: List[str] = field(default_factory=list)                   # "bear" (watch CRTH) / "bull" (watch CRTL)
    status: str = "armed"           # armed · tbs · entered · no-entry · expired
    inside_bar: bool = False        # 2nd candle was an inside bar → the 3rd candle is the manipulation
    live: bool = False              # manipulation candle still forming

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.update(time=hm(self.k_ts), win_time=hm(self.win_ts), chart_from=chart_time(self.k_ts),
                 chart_to=chart_time(self.win_end))
        return d


def _end(ts: int, tf: str) -> int:
    """Close time of the tf candle starting at ts (session-clipped; 1D = that day's 15:30)."""
    _, close = session_bounds(to_ist(ts).date())
    if tf == "1D":
        return int(close)
    return int(min(ts + TF_SECONDS[tf], close))


def _thick(b: Bars, i: int, min_ratio: float) -> bool:
    rng = b.h[i] - b.l[i]
    return rng > 0 and abs(b.c[i] - b.o[i]) / rng >= min_ratio


def _fmt(x: float) -> str:
    return f"{x:,.1f}"


# ═══════════════════════════════════════════════════════════════════════════
# HTF — CRT candle + keylevel
# ═══════════════════════════════════════════════════════════════════════════
def keylevels(an: Analysis, k: int) -> List[Dict[str, Any]]:
    """Keylevels of the CRT candle k, using only information known when candle k closed."""
    b = an.bars
    out: List[Dict[str, Any]] = []
    hi, lo = float(b.h[k]), float(b.l[k])
    # OHP / OLP — a confirmed swing (pivot known before k) that nothing took before k, taken by candle k
    for s in an.swings:
        if s.i >= k or s.confirm_i >= k:
            continue
        if s.kind == "H" and hi > s.price and not (b.h[s.i + 1:k] > s.price).any():
            out.append({"kind": "OHP", "dir": "bear", "price": round(s.price, 2), "ts": int(s.ts),
                        "text": f"OHP — old high {_fmt(s.price)} ({hm(s.ts)}) purged"})
        if s.kind == "L" and lo < s.price and not (b.l[s.i + 1:k] < s.price).any():
            out.append({"kind": "OLP", "dir": "bull", "price": round(s.price, 2), "ts": int(s.ts),
                        "text": f"OLP — old low {_fmt(s.price)} ({hm(s.ts)}) purged"})
    # OB — validated before k, not broken before k, candle k trades into it
    for ob in an.obs:
        if ob.kind != "OB" or ob.created_i >= k or (ob.broken_i is not None and ob.broken_i < k):
            continue
        if hi >= ob.bottom and lo <= ob.top:
            out.append({"kind": "OB", "dir": ob.dir, "top": round(ob.top, 2), "bottom": round(ob.bottom, 2),
                        "ts": int(ob.ts), "text": f"OB — {'supply' if ob.dir == 'bear' else 'demand'} "
                                                  f"{_fmt(ob.bottom)}–{_fmt(ob.top)}"})
    # FVG — known before k, still open at k−1, candle k trades into it
    for g in an.fvgs:
        if g.i >= k or g.state_at(k - 1) in ("filled", "invalid"):
            continue
        if hi >= g.bottom and lo <= g.top:
            out.append({"kind": "FVG", "dir": g.dir, "top": round(g.top, 2), "bottom": round(g.bottom, 2),
                        "ts": int(g.ts), "text": f"FVG — {'bearish' if g.dir == 'bear' else 'bullish'} "
                                                 f"{_fmt(g.bottom)}–{_fmt(g.top)}"})
    # RB — rejection block: the long-wick part of a confirmed swing candle, not closed through before k
    wr = float(R().get("rb_min_wick_ratio", 0.5))
    for s_ in an.swings:
        if s_.i >= k or s_.confirm_i >= k:
            continue
        i = s_.i
        rng = float(b.h[i] - b.l[i])
        if rng <= 0:
            continue
        if s_.kind == "H":
            top, bot = float(b.h[i]), float(max(b.o[i], b.c[i]))
            if (top - bot) / rng < wr or (b.c[i + 1:k] > top).any():
                continue
            d = "bear"
        else:
            top, bot = float(min(b.o[i], b.c[i])), float(b.l[i])
            if (top - bot) / rng < wr or (b.c[i + 1:k] < bot).any():
                continue
            d = "bull"
        if hi >= bot and lo <= top:
            out.append({"kind": "RB", "dir": d, "top": round(top, 2), "bottom": round(bot, 2), "ts": int(s_.ts),
                        "text": f"RB — {'upper' if d == 'bear' else 'lower'}-wick rejection block "
                                f"{_fmt(bot)}–{_fmt(top)} ({hm(s_.ts)})"})
    # keep the nearest of each (kind, dir) so the card stays short
    seen, keep = set(), []
    for x in sorted(out, key=lambda z: -int(z["ts"])):
        if (x["kind"], x["dir"]) not in seen:
            seen.add((x["kind"], x["dir"]))
            keep.append(x)
    return keep


def crt_candles(an: Analysis, pair: str, htf: str, ltf: str, lookback: int) -> List[CRTRange]:
    C = R()
    b = an.bars
    out: List[CRTRange] = []
    for k in range(max(1, len(b) - lookback), len(b)):
        rng = float(b.h[k] - b.l[k])
        if rng <= 0:
            continue
        br = abs(float(b.c[k] - b.o[k])) / rng
        ra = rng / float(an.atr[k]) if an.atr[k] > 0 else 0.0
        if br < float(C.get("min_body_ratio", 0.5)) or ra < float(C.get("min_range_atr", 0.8)):
            continue
        kl = keylevels(an, k)
        biases = sorted({x["dir"] for x in kl})
        if not biases and C.get("require_keylevel", True):
            continue
        k_ts = int(b.ts[k])
        out.append(CRTRange(pair, htf, ltf, k_ts, _end(k_ts, htf), 0, 0, round(float(b.h[k]), 2),
                            round(float(b.l[k]), 2), round(float((b.h[k] + b.l[k]) / 2), 2), round(br, 2),
                            round(ra, 2), ra >= float(C.get("aplus_range_atr", 1.2)), kl, biases))
    return out


# ═══════════════════════════════════════════════════════════════════════════
# LTF — TBS → Model #1 → entry
# ═══════════════════════════════════════════════════════════════════════════
def _scan(cr: CRTRange, bias: str, lb: Bars, win: List[int], deadline: int) -> Dict[str, Any]:
    """win = LTF bar indices of the manipulation candle (closed only); the entry may come until LTF bar `deadline`.
    Returns the TBS → entry-model story. The first model to confirm wins."""
    C = R()
    pen = float(C.get("tbs_min_pen_pts", 0.5))
    m1r = float(C.get("m1_min_body_ratio", 0.5))
    models = set(C.get("models", ["M1", "M2", "M3"]))
    sell = bias == "bear"
    lv = cr.crth if sell else cr.crtl
    res: Dict[str, Any] = {"bias": bias, "level": lv, "tbs": None, "m1": None, "entry": None, "inside": 0,
                           "model": None, "model_px": None}
    tbs = next((t for t in win if (lb.c[t] > lv + pen if sell else lb.c[t] < lv - pen)), None)
    if tbs is None:
        return res
    cap = int(C.get("aplus_inside_max", 6)) + 1                  # counting past the A+ limit says nothing more
    n, j = 0, tbs - 1
    while j >= 0 and (lb.c[j] <= lv if sell else lb.c[j] >= lv) and n < cap:
        n, j = n + 1, j - 1
    res.update(tbs=tbs, inside=n, inside_txt=f"{n}+" if n >= cap else str(n),
               aplus_tbs=int(C.get("aplus_inside_min", 2)) <= n <= int(C.get("aplus_inside_max", 6)))
    # Model #3 level: the last LTF swing low (sell) / high (buy) known before the TBS
    piv = pivots(lb, int(C.get("m3_pivot_n", 2)))
    sw = [x for x in piv if x.confirm_i <= tbs and x.i < tbs and x.kind == ("L" if sell else "H")]
    bos = sw[-1].price if sw and "M3" in models else None
    res["bos_level"] = bos
    fvg_min = float(C.get("m2_min_gap_pts", 1.0))
    want_up = sell                                   # Model #1 is a BUY candle for a sell, a SELL candle for a buy
    is_m1 = lambda t: _thick(lb, t, m1r) and ((lb.c[t] > lb.o[t]) == want_up)   # noqa: E731
    m = tbs if is_m1(tbs) else None
    cap_bars = C.get("m1_max_bars")
    last = min(len(lb) - 1, deadline, tbs + int(cap_bars) if cap_bars else 10 ** 9)
    for t in range(tbs + 1, last + 1):
        hit = None
        if "M1" in models and m is not None and (lb.c[t] < lb.l[m] if sell else lb.c[t] > lb.h[m]):
            hit = ("M1", float(lb.l[m] if sell else lb.h[m]))
        elif "M2" in models and t - 2 > tbs - 1 and t >= 2 and \
                ((lb.l[t - 2] - lb.h[t] >= fvg_min) if sell else (lb.l[t] - lb.h[t - 2] >= fvg_min)):
            hit = ("M2", float(lb.h[t] if sell else lb.l[t]))
        elif bos is not None and (lb.c[t] < bos if sell else lb.c[t] > bos):
            hit = ("M3", float(bos))
        if hit:
            res.update(m1=m if hit[0] == "M1" else t, entry=t, model=hit[0], model_px=hit[1],
                       m2_gap=((float(lb.h[t]), float(lb.l[t - 2])) if sell else (float(lb.h[t - 2]), float(lb.l[t])))
                       if hit[0] == "M2" else None)
            return res
        if is_m1(t):
            m = t
    res["m1"] = m
    if m is not None:
        res["model"] = "M1"
    return res


def trend_at(bias_an: Optional[Analysis], ts: int) -> str:
    """Structure trend of the bias timeframe as it was known at ts (last BOS/MSS whose candle had CLOSED)."""
    if bias_an is None:
        return "none"
    step = TF_SECONDS[bias_an.tf]
    ev = [e for e in bias_an.events if int(e.ts) + step <= ts]
    return ev[-1].dir if ev else "none"


def evaluate_pair(pair: Dict[str, Any], htf_rows: List[Dict[str, float]], ltf_rows: List[Dict[str, float]],
                  now: float, an: Optional[Analysis] = None, bias_an: Optional[Analysis] = None) -> Dict[str, Any]:
    """htf_rows / ltf_rows: CLOSED candles only (the caller filters). Pure: same input → same output.
    bias_an: the desk's bias-timeframe analysis (15m) — used only if crt_tbs.require_bias_alignment."""
    C = R()
    htf, ltf, name = pair["htf"], pair["ltf"], pair.get("name", f"{pair['htf']}→{pair['ltf']}")
    pid = f"{htf}>{ltf}"
    out: Dict[str, Any] = {"pair": pid, "name": name, "htf": htf, "ltf": ltf, "ranges": [], "events": [],
                           "signals": [], "message": ""}
    htf_rows = [r for r in htf_rows if r.get("closed", True)]
    ltf_rows = [r for r in ltf_rows if r.get("closed", True)]
    an = an or analyze(htf_rows, htf)
    if an is None or len(ltf_rows) < 5:
        out["message"] = f"not enough {htf} / {ltf} history yet"
        return out
    lb = Bars.from_rows(ltf_rows)
    lstep = TF_SECONDS[ltf]
    ranges = crt_candles(an, pid, htf, ltf, int(C.get("htf_lookback", 12)))
    S = config.rules()["session"]
    P = C.get("risk", {}).get(pid, {})
    for cr in ranges:
        # the manipulation candle = the HTF bucket of the first LTF candle after the CRT candle closed
        after = np.nonzero(lb.ts >= cr.k_end)[0]
        if len(after) == 0:
            cr.win_ts = int(bucket_start(cr.k_end, htf)) if htf != "1D" else cr.k_end
            cr.win_end, cr.live, cr.status = cr.win_ts + TF_SECONDS.get(htf, 0), True, "armed"
            out["ranges"].append(cr.to_dict())
            continue
        buckets: List[int] = []
        for t in after:
            bb = int(bucket_start(int(lb.ts[t]), htf))
            if not buckets or buckets[-1] != bb:
                buckets.append(bb)
            if len(buckets) > 3:
                break
        w0 = buckets[0]
        # INSIDE-BAR CRT: a closed 2nd candle that stayed inside the CRT range manipulated nothing → use the 3rd
        hb = np.nonzero(an.bars.ts == w0)[0]
        if C.get("inside_bar_crt", True) and len(hb) and len(buckets) > 1 and \
                an.bars.h[hb[0]] <= cr.crth and an.bars.l[hb[0]] >= cr.crtl:
            cr.inside_bar = True
            w0 = buckets[1]
        cr.win_ts, cr.win_end = w0, _end(w0, htf)
        win = [int(t) for t in after if int(bucket_start(int(lb.ts[t]), htf)) == w0]
        nxt = [b_ for b_ in buckets if b_ > w0]
        dl_ts = _end(nxt[0], htf) if (nxt and C.get("entry_next_htf_candle", True)) else cr.win_end
        dl = [int(t) for t in after if int(lb.ts[t]) + lstep <= dl_ts]
        deadline = dl[-1] if dl else (win[-1] if win else -1)
        cr.live = now < cr.win_end
        best = "armed" if cr.live else "expired"
        for bias in cr.biases:
            r = _scan(cr, bias, lb, win, max(deadline, win[-1] if win else -1))
            if r["tbs"] is not None:
                out["events"].append(_ev("TBS", cr, r, lb, r["tbs"], ltf))
                best = "tbs" if best in ("armed", "expired") else best
            if r["m1"] is not None:
                out["events"].append(_ev("M1", cr, r, lb, r["m1"], ltf))
            if r["entry"] is None:
                if r["tbs"] is not None and not cr.live and best == "tbs":
                    best = "no-entry"
                continue
            best = "entered"
            out["events"].append(_ev("ENTRY", cr, r, lb, r["entry"], ltf))
            out["signals"].append(_signal(cr, r, lb, ltf, name, S, P, C, bias_an))
        cr.status = best
        out["ranges"].append(cr.to_dict())
    out["ranges"] = out["ranges"][-int(C.get("max_ranges_shown", 6)):]
    return out


def _ev(kind: str, cr: CRTRange, r: Dict[str, Any], lb: Bars, t: int, ltf: str) -> Dict[str, Any]:
    sell = r["bias"] == "bear"
    ts = int(lb.ts[t])
    if kind == "TBS":
        px = float(lb.h[t] if sell else lb.l[t])
        text = (f"TBS: {ltf} body closed {'above CRTH' if sell else 'below CRTL'} {_fmt(r['level'])} "
                f"({r['inside_txt']} inside · {'A+' if r.get('aplus_tbs') else 'not A+'})")
    elif kind == "M1":
        mdl = r.get("model") or "M1"
        px = float(r.get("model_px") or (lb.l[t] if sell else lb.h[t]))
        text = {"M1": f"Model #1 (order block): {'buy' if sell else 'sell'} candle, trigger "
                      f"{'below' if sell else 'above'} {_fmt(px)}",
                "M2": f"Model #2 (FVG): {'bearish' if sell else 'bullish'} gap formed after the TBS",
                "M3": f"Model #3 (BOS): close {'below swing low' if sell else 'above swing high'} {_fmt(px)}"}[mdl]
    else:
        px = float(lb.c[t])
        text = f"Entry: close {_fmt(px)} — Model #{(r.get('model') or 'M1')[1]} → {'SELL' if sell else 'BUY'}"
    return {"id": f"CRT|{kind}|{cr.pair}|{cr.k_ts}|{r['bias']}", "kind": f"CRT_{kind}", "ts": ts, "time": hm(ts),
            "model": r.get("model") or "M1",
            "chart_time": chart_time(ts), "dir": "bear" if sell else "bull", "price": round(px, 2), "text": text,
            "pair": cr.pair, "ltf": ltf, "crt_ts": cr.k_ts}


def _model_line(r: Dict[str, Any], lb: Bars, m: int, sell: bool) -> str:
    mdl = r["model"]
    if mdl == "M1":
        return (f"{hm(int(lb.ts[m]))} Model #1 (order block) — {'buy' if sell else 'sell'} candle "
                f"{'low' if sell else 'high'} {_fmt(float(lb.l[m] if sell else lb.h[m]))}")
    if mdl == "M2":
        lo, hi = r["m2_gap"]
        return f"{hm(int(lb.ts[r['entry']]))} Model #2 (FVG) — {'bearish' if sell else 'bullish'} gap {_fmt(lo)}–{_fmt(hi)}"
    return f"Model #3 (BOS) — last {'swing low' if sell else 'swing high'} before the TBS {_fmt(r['bos_level'])}"


def _signal(cr: CRTRange, r: Dict[str, Any], lb: Bars, ltf: str, name: str, S: Dict[str, Any],
            P: Dict[str, Any], C: Dict[str, Any], bias_an: Optional[Analysis] = None) -> Signal:
    sell = r["bias"] == "bear"
    t, m, tbs = r["entry"], r["m1"], r["tbs"]
    entry = round(float(lb.c[t]), 2)
    buf = float(C.get("sl_buffer_pts", 2.0))
    ext = float(lb.h[tbs:t + 1].max()) if sell else float(lb.l[tbs:t + 1].min())
    sl = round(ext + buf if sell else ext - buf, 2)
    tp1, tp2 = cr.mid, (cr.crtl if sell else cr.crth)
    risk = round(abs(sl - entry), 2)
    rr1 = round(abs(entry - tp1) / risk, 2) if risk > 0 else 0.0
    rr2 = round(abs(entry - tp2) / risk, 2) if risk > 0 else 0.0
    reasons: List[str] = []
    if (sell and entry <= tp1) or (not sell and entry >= tp1):
        reasons.append("entry is already past the CRT 50 % (TP-1) — nothing left to take")
    touched = (lb.l[tbs:t].min() <= tp1) if sell else (lb.h[tbs:t].max() >= tp1)
    if t > tbs and touched:
        reasons.append("price already reached the 50 % before the entry")
    if rr1 < float(C.get("min_rr_tp1", 1.0)):
        reasons.append(f"R:R to TP-1 {rr1:.2f} < {C.get('min_rr_tp1', 1.0)} (course: avoid low-R:R setups)")
    lo, hi = float(P.get("min_risk_pts", 5)), float(P.get("max_risk_pts", 60))
    if not lo <= risk <= hi:
        reasons.append(f"stop {risk:.1f} pts outside {lo:.0f}–{hi:.0f} for {cr.pair}")
    close_t = int(lb.ts[t]) + TF_SECONDS[ltf]
    if not in_windows(close_t - 1, S["trade_windows"]):
        reasons.append(f"{to_ist(close_t).strftime('%H:%M')} is outside the trade windows")
    if C.get("require_aplus_tbs") and not r.get("aplus_tbs"):
        reasons.append(f"TBS not A+ ({r['inside_txt']} candles inside, needs "
                       f"{C.get('aplus_inside_min', 2)}–{C.get('aplus_inside_max', 6)})")
    if C.get("require_aplus_crt") and not cr.aplus:
        reasons.append(f"CRT candle not thick enough for A+ ({cr.range_atr:.1f}× ATR)")
    tr = trend_at(bias_an, close_t)
    if C.get("require_bias_alignment") and tr == ("bull" if sell else "bear"):
        reasons.append(f"against the {bias_an.tf} structure ({tr}) — desk filter, not part of the course")
    kl = [x for x in cr.keylevels if x["dir"] == r["bias"]]
    story = [
        ("Inside-bar CRT: the 2nd candle stayed inside, the 3rd candle is the manipulation · " if cr.inside_bar else "")
        + f"{cr.htf} CRT candle {hm(cr.k_ts)}: body {cr.body_ratio:.0%} of range, {cr.range_atr:.1f}× ATR"
        f"{' (A+ thick)' if cr.aplus else ''} · CRTH {_fmt(cr.crth)} · 50 % {_fmt(cr.mid)} · CRTL {_fmt(cr.crtl)}",
        "Keylevel: " + "; ".join(x["text"] for x in kl) + f" → {'bearish' if sell else 'bullish'} bias",
        f"{hm(int(lb.ts[tbs]))} {ltf} TBS — body closed {'above CRTH' if sell else 'below CRTL'} "
        f"{_fmt(r['level'])} ({r['inside_txt']} candles inside — {'A+ TBS' if r.get('aplus_tbs') else 'not A+'})",
        _model_line(r, lb, m, sell),
        f"{hm(int(lb.ts[t]))} close {_fmt(entry)} → Model #{r['model'][1]} confirmed → "
        f"{'SELL' if sell else 'BUY'} · SL {_fmt(sl)} (extreme {'high' if sell else 'low'}) · "
        f"TP-1 {_fmt(tp1)} (50 %, book half, SL → breakeven) · TP-2 {_fmt(tp2)} ({'CRTL' if sell else 'CRTH'})",
    ]
    s = Signal(key=f"D|{cr.pair}|{'short' if sell else 'long'}|{cr.k_ts}", setup="D",
               name=f"CRT + TBS {cr.htf}→{ltf}", tf=ltf, side="short" if sell else "long", i=t, ts=int(lb.ts[t]),
               status="SKIP" if reasons else "TRADE", entry=entry, sl=sl, tp=tp2, risk=risk, rr=rr2,
               target_src=f"{'CRTL' if sell else 'CRTH'} TP-2", reasons=reasons, story=story)
    s.ctx = {"model": r["model"], "inside_bar": cr.inside_bar, "tp1": tp1, "rr1": rr1, "crt": {"htf": cr.htf, "k_ts": cr.k_ts, "crth": cr.crth, "crtl": cr.crtl,
                                             "mid": cr.mid, "keylevels": kl, "aplus": cr.aplus},
             "tbs_ts": int(lb.ts[tbs]), "m1_ts": int(lb.ts[m]), "aplus_tbs": bool(r.get("aplus_tbs")),
             "inside": r["inside"], "pair": cr.pair, "name": name, "bias_trend": tr}
    return s


def live(res: Dict[str, Any], last_ltf_ts: Optional[int]) -> List[Signal]:
    """Signals whose entry candle is the LTF candle that just closed — the only ones that may alert / paper-trade."""
    return [s for s in res.get("signals", []) if last_ltf_ts is not None and s.ts == last_ltf_ts]


def markers(res: Dict[str, Any], chart_tf: str, since_ts: int) -> List[Dict[str, Any]]:
    """TBS / Model #1 / entry markers aligned to the chart's own candles (a 1m event sits on its 5m candle)."""
    out = []
    lab = {"CRT_TBS": "TBS", "CRT_M1": "M", "CRT_ENTRY": "CRT▶"}
    for e in res.get("events", []):
        if e["ts"] < since_ts:
            continue
        t = chart_time(int(bucket_start(e["ts"], chart_tf))) if chart_tf != "1D" else e["chart_time"]
        txt = e.get("model", "M1") if e["kind"] == "CRT_M1" else lab[e["kind"]]
        out.append({"time": t, "kind": e["kind"], "dir": e["dir"], "price": e["price"],
                    "text": txt, "at": e["time"], "id": e["id"], "pair": e["pair"]})
    return out
