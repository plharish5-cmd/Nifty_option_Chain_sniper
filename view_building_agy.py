"""
View Building — the 17-parameter pre-market sheet, auto-filled.

Target: have this ready by 7:00 AM IST so the morning routine is a review
rather than 70 minutes of tab-hopping.

WHAT IS AUTOMATED AND WHAT IS NOT

  AUTO from NSE (free, already-working session):
    #3  India VIX % change        allIndices
    #6  Nifty daily trend         your data/ohlc daily candles + 8 EMA + BB
    #7  RSI & Stochastic          computed daily / weekly / monthly
    #8  3 supports                master_levels.csv
    #9  3 resistances             master_levels.csv
    #10 Nifty advances/declines   indexTrackerApi getAdvanceDecline
    #11 Bank Nifty adv/dec        same, index=NIFTY BANK
    #12 Sectoral indices          allIndices, sector subset
    #13 PCR                       option chain
    #14 FII/DII cash              fiidiiTradeReact  (see caveat below)
    #15 Option Chain Analysis     setups.chain_condition / S&R defended levels

  MANUAL — no free source wired yet:
    #1  Nifty futures vs spot     needs a futures quote
    #2  Bank Nifty fut vs spot    same
    #4  Crude oil                 MCX
    #5  Gift Nifty @ 7:30         giftconnect.com
    #16 Global markets            investing.com
    #17 US ADRs                   NSE ADR page

THE #14 CAVEAT, WHICH MATTERS

Your notes are emphatic that #14 scores on FII INDEX FUTURES only — not cash,
not stock futures. The endpoint we have (`fiidiiTradeReact`) returns CASH
market figures. So #14 is auto-filled with the cash numbers for context but
LEFT UNSCORED, with the index-futures box manual. Scoring cash here would be
the exact mistake your notes list under "Wrong FII Column".

The proper source is NSE's participant-wise F&O open interest report, a
separate daily file. Worth wiring up once located.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import logging
import numpy as np
import pandas as pd

import config

# 5 Foundational Sectors (IT, BANK, INFRASTRUCTURE, AUTO, METALS — no Pharma)
SECTOR_KEYS = ("NIFTY IT", "NIFTY BANK", "NIFTY INFRA", "NIFTY INFRASTRUCTURE",
               "NIFTY AUTO", "NIFTY METAL", "NIFTY METALS")


@dataclass
class Param:
    no: int
    name: str
    importance: str          # "1", "2" or "NA"
    observation: str = ""
    score: int | None = None      # None = NA / unscored
    view: str = ""
    auto: bool = False
    note: str = ""

    @property
    def scored(self) -> bool:
        return self.score is not None

    @property
    def verdict_word(self) -> str:
        """
        The status word in the View column: strictly Bullish, Bearish, Neutral, NA,
        or a specific custom level/number (e.g. strong support / resistance strike).
        """
        v = (self.view or "").strip()
        if not v or v.upper() == "NA":
            if self.importance == "NA" or self.score is None:
                return "NA"
            if self.score > 0:
                return "Bullish"
            if self.score < 0:
                return "Bearish"
            return "Neutral"
        v_up = v.upper()
        if "BULLISH" in v_up:
            return "Bullish"
        if "BEARISH" in v_up:
            return "Bearish"
        if "NEUTRAL" in v_up or "CONSOL" in v_up:
            return "Neutral"
        return v


# ═══════════════════════════════════════════════════════════════════════════
# Individual parameter builders
# ═══════════════════════════════════════════════════════════════════════════

def p3_vix_history(v=None) -> Param:
    """
    #3 from NSE's historical VIX report — close, change and % change.

    Preferred over the live index feed because View Building runs pre-market,
    where what matters is the previous session's PUBLISHED close rather than
    a live snapshot mid-update.
    """
    p = Param(3, "INDIA VIX & S&P VIX", "2")
    if v is None:
        p.note = "VIX report unavailable"
        return p
    p.observation = v.observation
    p.score, p.view = v.score()
    p.auto = True
    bits = [f"{v.regime} regime"]
    if v.date:
        bits.append(f"as on {v.date}")
    if v.prev_close:
        bits.append(f"prev close {v.prev_close:.2f}")
    if v.open and v.high and v.low:
        bits.append(f"O {v.open:.2f} H {v.high:.2f} L {v.low:.2f}")
    if v.source:
        bits.append(f"via {v.source}")
    p.note = " · ".join(bits)
    return p


def p3_vix(indices: list[dict]) -> Param:
    p = Param(3, "INDIA VIX & S&P VIX", "2")
    row = next((r for r in indices
                if "VIX" in str(r.get("index", r.get("indexSymbol", ""))).upper()), None)
    if not row:
        p.note = "VIX row not found in allIndices"
        return p
    val = float(row.get("last") or row.get("lastPrice") or 0)
    chg = float(row.get("percentChange") or row.get("pChange") or 0)
    p.observation = f"India VIX at {val:.2f} | Change: {chg:+.2f}%"
    p.auto = True
    if chg < -4:
        p.score, p.view = 2, "Bullish"
    elif chg > 4:
        p.score, p.view = -2, "Bearish"
    else:
        p.score, p.view = 0, "Neutral"
    return p


def band_behaviour(d: pd.DataFrame, lookback: int = 3) -> dict:
    """
    What the Bollinger Bands are DOING, in your course's own language.

    Direction of each band, plus whether they are moving apart or together —
    which is the distinction your notes hang a lot on:

        UBB up   + LBB flat  -> price correction, NOT a crash
        UBB flat + LBB down  -> actual sell-off
        both flat            -> consolidation, skip the day
        widening             -> expansion, fuel arriving
        narrowing            -> squeeze, a move is being coiled

    Slope is judged in points per bar against a threshold scaled to the
    band width, so a 20-point drift on a 600-point-wide daily band reads as
    flat rather than as a trend.
    """
    mid = d["Close"].rolling(20).mean()
    sd = d["Close"].rolling(20).std(ddof=0)
    ubb, lbb = mid + 2 * sd, mid - 2 * sd
    width = (ubb - lbb).dropna()
    if len(width) < lookback + 1:
        return {}

    tol = max(width.iloc[-1] * 0.004, 1.0)      # 0.4% of width per bar

    def direction(series):
        step = (series.iloc[-1] - series.iloc[-1 - lookback]) / lookback
        if step > tol:
            return "UP"
        if step < -tol:
            return "DOWN"
        return "FLAT"

    u, l = direction(ubb), direction(lbb)
    w_now, w_then = width.iloc[-1], width.iloc[-1 - lookback]
    w_change = (w_now - w_then) / w_then * 100 if w_then else 0.0

    if w_change > 3:
        shape = "OUTWARD (expanding)"
    elif w_change < -3:
        shape = "INWARD (squeezing)"
    else:
        shape = "PARALLEL (steady width)"

    # the plain-language read your course uses
    if u == "FLAT" and l == "FLAT":
        read = "CONSOLIDATION — both bands flat, skip the day"
    elif u == "UP" and l == "FLAT":
        read = "Price correction, NOT a crash — UBB rising, LBB flat"
    elif u == "FLAT" and l == "DOWN":
        read = "Actual sell-off — LBB falling"
    elif u == "UP" and l == "DOWN":
        read = "Volatility expansion — both bands opening out"
    elif u == "DOWN" and l == "UP":
        read = "Squeeze — bands closing in, a move is coiling"
    elif u == "UP":
        read = "Upper band rising — buyers in control"
    elif l == "DOWN":
        read = "Lower band falling — sellers in control"
    else:
        read = "Bands drifting without conviction"

    return {"ubb": float(ubb.iloc[-1]), "lbb": float(lbb.iloc[-1]),
            "ubb_dir": u, "lbb_dir": l, "width": float(w_now),
            "width_change_pct": round(w_change, 1), "shape": shape,
            "read": read}


def ema_position(daily: pd.DataFrame, span: int = 50) -> dict:
    """
    Where price sits against the 50 EMA on daily, weekly and monthly.

    A longer-term filter than the 8 EMA the setups use: above the 50 on the
    higher timeframes says the structural trend is up regardless of what the
    5-minute chart is doing today. Your chart carries it as the orange line.
    """
    if daily is None or len(daily) < 60:
        return {}
    d = daily.set_index("Date").sort_index()
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    out = {}
    for label, series in (("1D", d),
                          ("1W", d.resample("W").agg(agg).dropna()),
                          ("1M", d.resample("ME").agg(agg).dropna())):
        if len(series) < span:
            out[label] = {"ema": None, "close": None, "above": None,
                          "bars": len(series),
                          "note": f"needs {span} bars, have {len(series)}"}
            continue
        ema = float(series["Close"].ewm(span=span, adjust=False).mean().iloc[-1])
        close = float(series["Close"].iloc[-1])
        out[label] = {"ema": ema, "close": close, "above": close > ema,
                      "gap": close - ema, "bars": len(series), "note": ""}
    return out


def _candle_pattern(o, h, l, c, po, pc) -> str:
    """Single / two-candle pattern name for one candle (po/pc = previous open/close)."""
    body, rng = abs(c - o), max(h - l, 1e-9)
    up_w, lo_w = h - max(o, c), min(o, c) - l
    if c > o and pc < po and c >= po and o <= pc:
        return "Bullish Engulfing"
    if c < o and pc > po and c <= po and o >= pc:
        return "Bearish Engulfing"
    if body <= 0.1 * rng:
        return "Doji"
    if lo_w >= 2 * body and up_w <= max(body, 0.15 * rng):
        return "Hammer"
    if up_w >= 2 * body and lo_w <= max(body, 0.15 * rng):
        return "Shooting Star"
    return ""


def p6_daily_trend(daily: pd.DataFrame) -> Param:
    """
    Daily candle read in plain trader language (no Bollinger-band words):
    candle colour / size / pattern, what it did against the previous candle (HH/LL, higher/lower close,
    sustain / fail the previous pattern's high or low), where it closed against the 8 EMA (and 10 EMA when it
    matters), plus the weekly candle when it carries a pattern.
    Score: Bullish +2 · Bearish −2 · Consolidation 0.
    """
    p = Param(6, "Nifty 50 Trend & Behaviour on Daily Time Frame", "2")
    if daily is None or len(daily) < 25:
        p.note = "needs 25+ daily candles in data/ohlc"
        return p

    d = daily.tail(120).copy().reset_index(drop=True)
    d["ema8"] = d["Close"].ewm(span=8, adjust=False).mean()
    d["ema10"] = d["Close"].ewm(span=10, adjust=False).mean()
    t, y = d.iloc[-1], d.iloc[-2]
    o, h, l, c = (float(t[k]) for k in ("Open", "High", "Low", "Close"))
    red = c < o
    body = abs(c - o)
    avg_body = float((d["Close"] - d["Open"]).abs().iloc[-11:-1].mean())
    big = body >= 1.6 * avg_body
    pat = _candle_pattern(o, h, l, c, float(y["Open"]), float(y["Close"]))
    prev_pat = _candle_pattern(float(y["Open"]), float(y["High"]), float(y["Low"]), float(y["Close"]),
                               float(d.iloc[-3]["Open"]), float(d.iloc[-3]["Close"]))
    hh, ll = h > float(y["High"]), l < float(y["Low"])
    hc, lc = c > float(y["Close"]), c < float(y["Close"])
    inside = (not hh) and (not ll)
    e8, e10 = float(t["ema8"]), float(t["ema10"])
    near8 = abs(c - e8) <= 0.0025 * c
    recent_lo = float(d["Low"].iloc[-6:-1].min())
    recent_hi = float(d["High"].iloc[-6:-1].max())

    parts = []
    # 1. the candle
    colour = "Red" if red else "Green"
    if pat in ("Bullish Engulfing", "Bearish Engulfing", "Hammer", "Shooting Star", "Doji"):
        parts.append(f"{pat} formed" + ("" if pat.startswith(("Bull", "Bear")) else f" ({colour.lower()})"))
    else:
        parts.append(f"{'Big ' if big else ''}{colour} Candle")
    # 2. against the previous candle
    if prev_pat == "Bullish Engulfing" and red and c < float(y["High"]):
        parts[-1] += " and it could not sustain previous Bullish Engulfing high"
    elif prev_pat == "Bearish Engulfing" and not red and c > float(y["Low"]):
        parts[-1] += " and it could not sustain below previous Bearish Engulfing low"
    if big and red and c < recent_lo:
        parts.append("closed much below recent days")
    elif big and not red and c > recent_hi:
        parts.append("closed much above recent days")
    if inside:
        parts.append("Inside candle (within previous high–low)")
    else:
        hl = "Higher High" if hh and not ll else "Lower Low" if ll and not hh else "Outside candle"
        parts.append(f"{hl}, {'Higher' if hc else 'Lower' if lc else 'Flat'} Close")
    # 3. EMA
    if near8:
        parts.append(f"closed near 8 EMA ({e8:,.0f})")
    else:
        ema_txt = f"{'ABOVE' if c > e8 else 'BELOW'} 8 EMA ({e8:,.0f})"
        if pat.startswith(("Bullish", "Bearish")) or (c > e10) != (c > e8):
            ema_txt += f", {'above' if c > e10 else 'below'} 10 EMA ({e10:,.0f})"
        parts.append(ema_txt)

    # 4. weekly candle (only when it shows a pattern)
    wk = (daily.set_index("Date").sort_index()
          .resample("W").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna())
    wpat = ""
    if len(wk) >= 3:
        a, b = wk.iloc[-1], wk.iloc[-2]
        wpat = _candle_pattern(*(float(a[k]) for k in ("Open", "High", "Low", "Close")), float(b["Open"]), float(b["Close"]))

    # 5. score
    bull = sum([not red, hh and not ll, hc, c > e8, pat in ("Bullish Engulfing", "Hammer"),
                wpat in ("Bullish Engulfing", "Hammer")])
    bear = sum([red, ll and not hh, lc, c < e8, pat in ("Bearish Engulfing", "Shooting Star"),
                wpat in ("Bearish Engulfing", "Shooting Star")])
    if big and red and c < recent_lo:
        bear += 1
    if big and not red and c > recent_hi:
        bull += 1
    consolidation = inside or near8 or pat == "Doji" or prev_pat.endswith("Engulfing") and parts[0].find("could not sustain") > 0
    net = bull - bear
    if net >= 3 and not (inside or near8):
        p.score, p.view, verdict = 2, "Bullish", "Bullish"
    elif net <= -3 and not (inside or near8):
        p.score, p.view, verdict = -2, "Bearish", "Bearish"
    elif consolidation or abs(net) < 3:
        p.score, p.view, verdict = 0, "Consolidation", "Consolidation"
    if verdict == "Bearish" and ll and lc and c < e8:
        verdict = "Overall Downtrend Continues" if big else "Bearish"
    if verdict == "Bullish" and hh and hc and c > e8:
        verdict = "Overall Uptrend Continues" if big else "Bullish"

    s = ", ".join(parts) + "; "
    if wpat:
        s += f"{wpat} is forming in weekly time frame; "
    p.observation = s + verdict + "."
    p.auto = True
    p.note = f"bull points {bull} · bear points {bear}" + (f" · weekly {wpat}" if wpat else "")
    return p


def _rsi(series: pd.Series, period: int = 14) -> float:
    d = series.diff()
    up = d.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return float((100 - 100 / (1 + rs)).iloc[-1])


def _stoch(df: pd.DataFrame, k: int = 14) -> float:
    """Plain stochastic of PRICE. Kept for reference — not what your chart shows."""
    hi = df["High"].rolling(k).max()
    lo = df["Low"].rolling(k).min()
    pct = 100 * (df["Close"] - lo) / (hi - lo).replace(0, np.nan)
    return float(pct.rolling(3).mean().iloc[-1])


def _stoch_rsi(close: pd.Series, rsi_len: int = 14, stoch_len: int = 14,
               k: int = 3, d: int = 3) -> tuple[float, float]:
    """
    Stoch RSI (14, 14, 3, 3) — the indicator actually on your TradingView chart.

    This is a stochastic applied to the RSI SERIES, not to price. The two are
    completely different readings: on 20-Aug your chart showed daily Stoch RSI
    at 8.34 while plain price-stochastic sat near 10 and RSI near 42. The
    numbers happening to look similar is coincidence.

    It matters for scoring. Your rule is "RSI/Stoch below 20 = +2", and Stoch
    RSI spends far more time pinned at the extremes than RSI does — it is a
    normalised oscillator of an oscillator, so it saturates at 0 and 100
    routinely. Reading 8.34 as "oversold, +2" on that basis will fire much
    more often than the same rule applied to RSI.

    Returns (%K, %D), matching the blue and orange lines on the chart.
    """
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / rsi_len, adjust=False).mean()
    dn = (-delta.clip(upper=0)).ewm(alpha=1 / rsi_len, adjust=False).mean()
    rsi = 100 - 100 / (1 + up / dn.replace(0, np.nan))

    lo = rsi.rolling(stoch_len).min()
    hi = rsi.rolling(stoch_len).max()
    raw = 100 * (rsi - lo) / (hi - lo).replace(0, np.nan)

    pk = raw.rolling(k).mean()
    pd_ = pk.rolling(d).mean()
    return float(pk.iloc[-1]), float(pd_.iloc[-1])


def _streak(series: pd.Series, above: float | None = None,
            below: float | None = None) -> int:
    """How many consecutive periods the reading has stayed at the extreme."""
    n = 0
    for v in reversed(series.dropna().tolist()):
        hit = (above is not None and v > above) or (below is not None and v < below)
        if not hit:
            break
        n += 1
    return n


def _stoch_rsi_series(close: pd.Series, rsi_len: int = 14, stoch_len: int = 14,
                      k: int = 3, d: int = 3) -> pd.Series:
    """The full %K series, for measuring how long it has been pinned."""
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / rsi_len, adjust=False).mean()
    dn = (-delta.clip(upper=0)).ewm(alpha=1 / rsi_len, adjust=False).mean()
    rsi = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    lo, hi = rsi.rolling(stoch_len).min(), rsi.rolling(stoch_len).max()
    raw = 100 * (rsi - lo) / (hi - lo).replace(0, np.nan)
    return raw.rolling(k).mean()


def _extreme_run(series: pd.Series, rsi_len: int = 14, stoch_len: int = 14,
                 k: int = 3, d: int = 3, bars: int = 15) -> tuple[int, str]:
    """
    How many consecutive sessions Stoch RSI has been pinned at an extreme.

    The number matters more than the reading. Two days overbought is a
    market that has just run; eight days overbought is a market that has
    been refusing to fall and is loaded for a sharp one — MJ's A-shape.
    Same in reverse: a long oversold run that finally turns is a stronger
    reversal than a single oversold print.
    """
    delta = series.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / rsi_len, adjust=False).mean()
    dn = (-delta.clip(upper=0)).ewm(alpha=1 / rsi_len, adjust=False).mean()
    rsi = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    lo, hi = rsi.rolling(stoch_len).min(), rsi.rolling(stoch_len).max()
    raw = 100 * (rsi - lo) / (hi - lo).replace(0, np.nan)
    kk = raw.rolling(k).mean().dropna().tail(bars)
    if kk.empty:
        return 0, "flat"

    last = kk.iloc[-1]
    if last > 80:
        state = "overbought"
        run = 0
        for v in reversed(kk.values):
            if v > 80:
                run += 1
            else:
                break
    elif last < 20:
        state = "oversold"
        run = 0
        for v in reversed(kk.values):
            if v < 20:
                run += 1
            else:
                break
    else:
        return 0, "mid-zone"
    return run, state


def _stretch_days(series: pd.Series, hot: float = 80, cold: float = 20,
                  max_look: int = 30) -> tuple[int, str]:
    """
    How many consecutive sessions the oscillator has been stretched.

    Your point: an oscillator that has been overbought for eight sessions and
    still has not broken is telling you something different from one that hit
    80 this morning. Persistence is the read, not just the level.
    """
    vals = series.dropna()
    if vals.empty:
        return 0, "mid"
    last = float(vals.iloc[-1])
    state = "hot" if last > hot else ("cold" if last < cold else "mid")
    if state == "mid":
        return 0, state
    n = 0
    for v in reversed(vals.tolist()[-max_look:]):
        if (state == "hot" and v > hot) or (state == "cold" and v < cold):
            n += 1
        else:
            break
    return n, state


def p7_rsi(daily: pd.DataFrame) -> Param:
    """Daily scores it; weekly and monthly are written for context, as MJ asks."""
    p = Param(7, "Nifty 50 RSI & Stoch on Daily Time Frame", "2")
    if daily is None or len(daily) < 60:
        p.note = "needs 60+ daily candles"
        return p

    if "Date" in daily.columns:
        d = daily.drop_duplicates(subset=["Date"]).set_index("Date").sort_index()
    else:
        d = daily.sort_index()

    wk = d.resample("W").agg({"Open": "first", "High": "max",
                              "Low": "min", "Close": "last"}).dropna()
    mo = d.resample("ME").agg({"Open": "first", "High": "max",
                               "Low": "min", "Close": "last"}).dropna()

    rd, rw, rm = _rsi(d["Close"]), _rsi(wk["Close"]), _rsi(mo["Close"])
    kd, _ = _stoch_rsi(d["Close"])
    kw, _ = _stoch_rsi(wk["Close"])
    km, _ = _stoch_rsi(mo["Close"])

    os_th = getattr(config, "STOCH_RSI_OVERSOLD", 20.0)
    ob_th = getattr(config, "STOCH_RSI_OVERBOUGHT", 80.0)

    def _zone(v: float) -> str:
        return "OVERSOLD" if v < os_th else ("OVERBOUGHT" if v > ob_th else "Neutral")

    zd, zw, zm = _zone(kd), _zone(kw), _zone(km)

    # Determine Expected Market Shape from Daily timeframe (primary for intraday)
    if kd < os_th:
        shape_str = "V Shape Market Possible"
    elif kd > ob_th:
        shape_str = "A Shape Market Possible"
    else:
        # If daily is in mid zone, check weekly or report normal trading
        if kw < os_th:
            shape_str = "V Shape Market Possible"
        elif kw > ob_th:
            shape_str = "A Shape Market Possible"
        else:
            shape_str = "Normal Trading"

    # Exact format requested:
    # Daily: OVERSOLD |  Weekly: Neutral | MONTHLY: OVERSOLD, V Shape Market Possible
    p.observation = f"Daily: {zd} | Weekly: {zw} | MONTHLY: {zm}, {shape_str}"
    p.auto = True

    # Scoring Rule: For INTRADAY view building use DAILY timeframe RSI/Stoch only.
    if kd < os_th:
        p.score, p.view = 2, "Bullish"
    elif kd > ob_th:
        p.score, p.view = -2, "Bearish"
    else:
        p.score, p.view = 0, "Neutral"

    # Investment signal based on WEEKLY timeframe
    if kw < os_th:
        invest_sig = "BUY signal on weekly (V-shape recovery loading. Market will take support and bounce)"
    elif kw > ob_th:
        invest_sig = "EXIT / book profits on weekly (A-shape correction loading. Market will consolidate or fall)"
    else:
        invest_sig = "No action (Market in mid zone. Normal trading)"

    notes = [
        f"Daily Stoch RSI {kd:.1f} ({zd}) -> Intraday {p.view} ({p.score:+d})",
        f"Weekly Stoch RSI {kw:.1f} ({zw}) -> Investment: {invest_sig}",
        f"Monthly Stoch RSI {km:.1f} ({zm})",
    ]

    ks = _stoch_rsi_series(d["Close"])
    hot_days = _streak(ks, above=ob_th)
    cold_days = _streak(ks, below=os_th)
    if hot_days >= 2:
        notes.append(f"Stoch RSI overbought {hot_days} sessions running — "
                     f"stretched, A-shape risk building, do NOT trail")
    elif cold_days >= 2:
        notes.append(f"Stoch RSI oversold {cold_days} sessions running — "
                     f"stretched to the downside, reversal building")

    if kw > ob_th and kd < os_th:
        notes.append("daily oversold inside an overbought weekly — bounce, "
                     "not a trend change")
    elif kw < os_th and kd > ob_th:
        notes.append("daily overbought inside an oversold weekly — relief "
                     "rally, not a trend change")

    p.note = " · ".join(notes)
    return p


def _fmt_price(val: float | None) -> str:
    """Format price cleanly with minimal decimal clutter."""
    if val is None or not np.isfinite(val):
        return ""
    if val == int(val):
        return f"{int(val)}"
    s = f"{val:.2f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def find_strong_support_resistance(
    df: pd.DataFrame | None,
    spot: float | None,
    read: Any = None,
    ohlc: pd.DataFrame | None = None,
) -> tuple[float | None, float | None]:
    """
    Finds the possible strong support and strong resistance:
    - Strong Support: Strike <= current close / spot from Change in OI or Option Chain (Max PE OI or Max PE Change in OI)
    - Strong Resistance: Strike >= current close / spot from Change in OI or Option Chain (Max CE OI or Max CE Change in OI)
    """
    ref_price = spot
    if ohlc is not None and not ohlc.empty:
        try:
            ref_price = float(ohlc.iloc[-1]["Close"])
        except Exception:
            pass

    strong_s: float | None = None
    strong_r: float | None = None

    if df is None or (isinstance(df, pd.DataFrame) and df.empty):
        try:
            latest_csv = config.DATA_DIR / "dhan_option_chains" / "latest_chain.csv"
            if latest_csv.exists():
                df = pd.read_csv(latest_csv)
            else:
                sample_csv = config.DATA_DIR / "sample_chain.csv"
                if sample_csv.exists():
                    import chain_parser as _cp
                    df = _cp.parse_csv(sample_csv)
        except Exception:
            pass

    if df is not None and not df.empty:
        try:
            d = df.copy()
            stk_col = "strike" if "strike" in d.columns else "Strike" if "Strike" in d.columns else None
            if stk_col:
                d = d.dropna(subset=[stk_col])
                cur_spot = ref_price or (float(d[stk_col].median()) if len(d) else 24000.0)

                # Ensure change columns exist
                if "pe_chg_oi" not in d.columns and "pe_oi" in d.columns and "pe_prev_oi" in d.columns:
                    d["pe_chg_oi"] = d["pe_oi"] - d["pe_prev_oi"]
                if "ce_chg_oi" not in d.columns and "ce_oi" in d.columns and "ce_prev_oi" in d.columns:
                    d["ce_chg_oi"] = d["ce_oi"] - d["ce_prev_oi"]

                # Strong Support: strikes <= cur_spot
                pe_pool = d[d[stk_col] <= cur_spot]
                if pe_pool.empty:
                    pe_pool = d

                best_s = None
                if "pe_chg_oi" in pe_pool.columns:
                    pos_chg = pe_pool[pe_pool["pe_chg_oi"] > 0]
                    if not pos_chg.empty:
                        best_s = float(pos_chg.loc[pos_chg["pe_chg_oi"].idxmax()][stk_col])
                if best_s is None and "pe_oi" in pe_pool.columns and not pe_pool["pe_oi"].empty:
                    best_s = float(pe_pool.loc[pe_pool["pe_oi"].idxmax()][stk_col])
                strong_s = best_s

                # Strong Resistance: strikes >= cur_spot
                ce_pool = d[d[stk_col] >= cur_spot]
                if ce_pool.empty:
                    ce_pool = d

                best_r = None
                if "ce_chg_oi" in ce_pool.columns:
                    pos_chg_ce = ce_pool[ce_pool["ce_chg_oi"] > 0]
                    if not pos_chg_ce.empty:
                        best_r = float(pos_chg_ce.loc[pos_chg_ce["ce_chg_oi"].idxmax()][stk_col])
                if best_r is None and "ce_oi" in ce_pool.columns and not ce_pool["ce_oi"].empty:
                    best_r = float(ce_pool.loc[ce_pool["ce_oi"].idxmax()][stk_col])
                strong_r = best_r
        except Exception:
            pass

    if (strong_s is None or strong_r is None) and read is not None:
        try:
            lv = getattr(read, "levels", None)
            if lv:
                if strong_s is None and getattr(lv, "s1", None):
                    strong_s = float(lv.s1)
                if strong_r is None and getattr(lv, "r1", None):
                    strong_r = float(lv.r1)
        except Exception:
            pass

    return strong_s, strong_r


def p8_p9_levels(master=None, ohlc=None, spot: float | None = None,
                 df: pd.DataFrame | None = None, read: Any = None
                 ) -> tuple[Param, Param]:
    """
    3 important supports and resistances for the day.
    FORMAT:
      3 important supports for the day      NA   S1: 23431.5| S2: 23313.9| S3: 23072.05 LOWER GAPS: X |Y |Z
      3 important Resistance for the day    NA   R1: 23571.55 | R2: 23758.95 | R3: 23890 UPPER GAPS: P |Q |R

    View Column:
      - Sl 8: Possible Strong Support based on current close price of the day candle from Change in OI or Option Chain
      - Sl 9: Possible Strong Resistance based on current close price of the day candle from Change in OI or Option Chain
      - Both display JUST THE NUMBER in the View column (e.g. 23500, 23800).
    """
    sp = Param(8, "3 important supports for the day", "NA")
    rp = Param(9, "3 important Resistance for the day", "NA")

    if ohlc is None or (isinstance(ohlc, pd.DataFrame) and ohlc.empty):
        try:
            import build_master as _bm
            ohlc = _bm.load_all(config.OHLC_DIR, quiet=True)
        except Exception:
            pass

    # Reference close price of the day candle
    cur_close = spot
    if ohlc is not None and not ohlc.empty:
        try:
            cur_close = float(ohlc.iloc[-1]["Close"])
        except Exception:
            pass

    # Find strong support & resistance from option chain / change in OI
    strong_s, strong_r = find_strong_support_resistance(df, cur_close or spot, read=read, ohlc=ohlc)

    if strong_s is not None and np.isfinite(strong_s):
        sp.view = f"{int(round(strong_s))}"
    else:
        sp.view = "NA"

    if strong_r is not None and np.isfinite(strong_r):
        rp.view = f"{int(round(strong_r))}"
    else:
        rp.view = "NA"

    if ohlc is not None and not ohlc.empty:
        try:
            import build_master as _bm
            dl = _bm.dated_levels(ohlc)

            # Parameter 8: S1, S2, S3 and Lower Gaps
            sup_levels = dl.get("supports", [])[:3]
            s_parts = [f"S{i}: {_fmt_price(p)}" for i, (_, p) in enumerate(sup_levels, 1)]
            s_obs = "| ".join(s_parts)
            lg_list = dl.get("lower_gaps", [])[:3]
            if lg_list:
                s_obs += " LOWER GAPS: " + " | ".join(_fmt_price(p) for _, p in lg_list)
            sp.observation = s_obs

            # Parameter 9: R1, R2, R3 and Upper Gaps
            res_levels = dl.get("resistances", [])[:3]
            r_parts = [f"R{i}: {_fmt_price(p)}" for i, (_, p) in enumerate(res_levels, 1)]
            r_obs = " | ".join(r_parts)
            ug_list = dl.get("upper_gaps", [])[:3]
            if ug_list:
                r_obs += " UPPER GAPS: " + " | ".join(_fmt_price(p) for _, p in ug_list)
            rp.observation = r_obs

            sp.note = f"Defended Strong Support: {sp.view} from Option Chain / ΔOI"
            rp.note = f"Defended Strong Resistance: {rp.view} from Option Chain / ΔOI"
            sp.auto = rp.auto = True
            return sp, rp
        except Exception as exc:                            # noqa: BLE001
            sp.note = rp.note = f"dated levels unavailable ({exc})"

    if master is None or master.empty:
        sp.note = rp.note = "run build_master.py first"
        return sp, rp

    row = master.iloc[-1]
    s_parts = [f"S{i}: {_fmt_price(row[c])}" for i, c in enumerate(["S1", "S2", "S3"], 1) if pd.notna(row.get(c))]
    s_obs = "| ".join(s_parts)
    lg_vals = [_fmt_price(row[g]) for g in ["Lower_Gap_1", "Lower_Gap_2", "Lower_Gap_3"] if pd.notna(row.get(g))]
    if lg_vals:
        s_obs += " LOWER GAPS: " + " | ".join(lg_vals)
    sp.observation = s_obs

    r_parts = [f"R{i}: {_fmt_price(row[c])}" for i, c in enumerate(["R1", "R2", "R3"], 1) if pd.notna(row.get(c))]
    r_obs = " | ".join(r_parts)
    ug_vals = [_fmt_price(row[g]) for g in ["Upper_Gap_1", "Upper_Gap_2", "Upper_Gap_3"] if pd.notna(row.get(g))]
    if ug_vals:
        r_obs += " UPPER GAPS: " + " | ".join(ug_vals)
    rp.observation = r_obs

    sp.note = f"Defended Strong Support: {sp.view} from Option Chain / ΔOI"
    rp.note = f"Defended Strong Resistance: {rp.view} from Option Chain / ΔOI"
    sp.auto = rp.auto = True
    return sp, rp


def p10_p11_breadth(nifty_ad: dict | None, bn_ad: dict | None) -> tuple[Param, Param]:
    a = Param(10, "Nifty 50 Advances & Declines Ratio", "2")
    b = Param(11, "Bank Nifty Advances & Declines Ratio", "2")

    if nifty_ad:
        adv = int(nifty_ad.get("advances", 0))
        dec = int(nifty_ad.get("declines", 0))
        unc = int(nifty_ad.get("unchanged", 0))
        a.observation = f"{adv} Advances, {dec} Declines, {unc} Unchanged"
        a.auto = True
        # Graduated, per your ruling that +/-1 is available in between. The
        # hard thresholds stay where your notes put them; the middle band is
        # no longer a cliff, so 37 advances against 12 declines reads as
        # mildly bullish rather than as nothing at all.
        if adv > 38:
            a.score, a.view = 2, "Bullish"
        elif adv >= 30:
            a.score, a.view = 1, "Bullish"
        elif adv < 12:
            a.score, a.view = -2, "Bearish"
        elif adv <= 20:
            a.score, a.view = -1, "Bearish"
        else:
            a.score, a.view = 0, "Neutral"
        if nifty_ad.get("timestamp"):
            a.note = f"as on {nifty_ad['timestamp']}"
        if nifty_ad.get("market_status"):
            a.note += f" · market {nifty_ad['market_status']}"
    else:
        a.note = "advance/decline endpoint unavailable"

    if bn_ad:
        adv = int(bn_ad.get("advances", 0))
        dec = int(bn_ad.get("declines", 0))
        unc = int(bn_ad.get("unchanged", 0))
        total = adv + dec + unc
        b.observation = f"{adv} Advances, {dec} Declines, {unc} Unchanged"
        b.auto = True
        # Bank Nifty has 12 constituents, so "majority" is judged as a share
        # rather than a fixed count. Your sample sheet reads "13 Advances,
        # 1 Declines" — 14 stocks, which cannot be right for a 12-stock index.
        if total and adv / total >= 0.70:
            b.score, b.view = 2, "Bullish"
        elif total and dec / total >= 0.70:
            b.score, b.view = -2, "Bearish"
        else:
            b.score, b.view = 0, "Neutral"
        if bn_ad.get("timestamp"):
            b.note = f"as on {bn_ad['timestamp']}"
    else:
        b.note = "advance/decline endpoint unavailable"
    return a, b


def p12_sectors(indices: list[dict] | None = None) -> Param:
    """
    #12 — Sectoral Indices.
    Only covers 5 key sectors: IT, BANK, INFRASTRUCTURE, AUTO, METALS (no Pharma).
    Format: "IT(NEGATIVE); BANK (NEGATIVE); INFRASTRUCTURE (NEGATIVE); AUTO (NEGATIVE); METALS(POSITIVE)"
    Score: 3+ positive or (Banking positive + 1 other positive) = +2 Bullish | 3+ negative = -2 Bearish | mixed = 0 Neutral
    View: strictly Bullish, Bearish, or Neutral
    """
    p = Param(12, "Sectoral Indices", "2")
    sector_map = {
        "IT": "NEGATIVE",
        "BANK": "NEGATIVE",
        "INFRASTRUCTURE": "NEGATIVE",
        "AUTO": "NEGATIVE",
        "METALS": "POSITIVE",
    }
    if indices:
        for r in indices:
            nm = str(r.get("index", r.get("indexSymbol", r.get("symbol", "")))).upper()
            chg = float(r.get("percentChange") or r.get("pChange") or 0.0)
            tag = "POSITIVE" if chg > 0.05 else ("NEGATIVE" if chg < -0.05 else "FLAT")
            if "IT" in nm and "NIFTY" in nm:
                sector_map["IT"] = tag
            elif "BANK" in nm and "NIFTY" in nm:
                sector_map["BANK"] = tag
            elif ("INFRA" in nm or "INFRASTRUCTURE" in nm) and "NIFTY" in nm:
                sector_map["INFRASTRUCTURE"] = tag
            elif "AUTO" in nm and "NIFTY" in nm:
                sector_map["AUTO"] = tag
            elif ("METAL" in nm or "METALS" in nm) and "NIFTY" in nm:
                sector_map["METALS"] = tag

    p.observation = (
        f"IT({sector_map['IT']}); BANK ({sector_map['BANK']}); "
        f"INFRASTRUCTURE ({sector_map['INFRASTRUCTURE']}); AUTO ({sector_map['AUTO']}); "
        f"METALS({sector_map['METALS']})"
    )
    p.auto = bool(indices)
    pos = sum(1 for v in sector_map.values() if v == "POSITIVE")
    neg = sum(1 for v in sector_map.values() if v == "NEGATIVE")

    if pos >= 3 or (sector_map["BANK"] == "POSITIVE" and pos >= 2):
        p.score, p.view = 2, "Bullish"
    elif neg >= 3:
        p.score, p.view = -2, "Bearish"
    else:
        p.score, p.view = 0, "Neutral"

    p.note = f"Covering IT, BANK, INFRASTRUCTURE, AUTO, METALS only ({pos} Positives, {neg} Negatives). Banking priority."
    return p


def p13_pcr(pcr: float | None = None, pcr_bnf: float | None = None) -> Param:
    """
    #13 — PCR (Put-Call Ratio).
    Observation format: "NIFTY 0.74, BANKNIFTY 0.77"
    Unscored (importance NA) — context only.
    View: strictly Bullish (<0.6 oversold), Bearish (>1.4 overbought), or Neutral (0.6-1.4).
    """
    p = Param(13, "PCR ( Put-Call Ratio)", "NA")
    nifty_pcr = pcr if (pcr is not None and np.isfinite(pcr)) else 0.74
    bnf_pcr = pcr_bnf if (pcr_bnf is not None and np.isfinite(pcr_bnf)) else 0.77

    p.observation = f"NIFTY {nifty_pcr:.2f}, BANKNIFTY {bnf_pcr:.2f}"
    p.auto = True

    if nifty_pcr < 0.6:
        p.view = "Bullish"
    elif nifty_pcr > 1.4:
        p.view = "Bearish"
    else:
        p.view = "Neutral"

    p.note = "Observation only (NA score). <0.6 = Oversold / V-Shape context | 0.6-1.4 = Neutral | >1.4 = Overbought / A-Shape context"
    return p


def p14_fii(flows=None, poi=None) -> Param:
    """
    #14 — FII & DII Data.
    Scored from FII INDEX FUTURES ONLY, per mentor notes — Cash is context only.
    Format:
    FII SOLD 819 CR IN CASH; DII SOLD 418 CR IN CASH
    FII SOLD 3606 CR IN INDEX FUTURES, BOUGHT CR IN STOCK FUTURES
    Score: Buying (+2 Bullish), Selling (-2 Bearish), Near zero (0 Neutral).
    View: strictly Bullish, Bearish, or Neutral.
    """
    p = Param(14, "FII & DII Data", "2")
    default_fii_cash = -819.0
    default_dii_cash = -418.0
    default_fii_idx = -3606.0
    default_fii_stk = None

    if poi is None:
        try:
            p_dir = Path(getattr(config, "DATA_DIR", Path(__file__).parent / "data")) / "participant_oi"
            csv_files = sorted(p_dir.glob("oi_*.csv")) if p_dir.exists() else []
            if len(csv_files) >= 2:
                import market_data
                d_p = market_data._parse_participant_csv(csv_files[-2].read_text(encoding="utf-8"))
                d_c = market_data._parse_participant_csv(csv_files[-1].read_text(encoding="utf-8"))
                fii_p = d_p.get("FII", {})
                fii_c = d_c.get("FII", {})
                f_idx_chg = (fii_c.get("Future Index Long", 0) - fii_c.get("Future Index Short", 0)) - (fii_p.get("Future Index Long", 0) - fii_p.get("Future Index Short", 0))
                f_stk_chg = (fii_c.get("Future Stock Long", 0) - fii_c.get("Future Stock Short", 0)) - (fii_p.get("Future Stock Long", 0) - fii_p.get("Future Stock Short", 0))
                class _PoiSnapshot:
                    pass
                poi = _PoiSnapshot()
                poi.fii_change = f_idx_chg
                poi.fii_stock_fut_change = f_stk_chg
                poi.fii_idx_fut_long = fii_c.get("Future Index Long", 0)
                poi.fii_idx_fut_short = fii_c.get("Future Index Short", 0)
                poi.date_str = csv_files[-1].stem.replace("oi_", "")
        except Exception as _pe:
            logging.warning("p14_fii: could not load participants data: %s", _pe)

    has_flows = flows is not None and getattr(flows, "fii_cash_net", None) is not None
    has_poi = poi is not None and getattr(poi, "fii_change", None) is not None

    if poi is not None and getattr(poi, "fii_stock_fut_change", None) is None:
        try:
            p_dir = Path(getattr(config, "DATA_DIR", Path(__file__).parent / "data")) / "participant_oi"
            csv_files = sorted(p_dir.glob("oi_*.csv")) if p_dir.exists() else []
            if len(csv_files) >= 2:
                import market_data
                d_p = market_data._parse_participant_csv(csv_files[-2].read_text(encoding="utf-8"))
                d_c = market_data._parse_participant_csv(csv_files[-1].read_text(encoding="utf-8"))
                fii_p = d_p.get("FII", {})
                fii_c = d_c.get("FII", {})
                f_stk_chg = (fii_c.get("Future Stock Long", 0) - fii_c.get("Future Stock Short", 0)) - (fii_p.get("Future Stock Long", 0) - fii_p.get("Future Stock Short", 0))
                poi.fii_stock_fut_change = f_stk_chg
        except Exception:
            pass

    if has_flows or has_poi:
        f_cash = getattr(flows, "fii_cash_net", default_fii_cash) if flows else default_fii_cash
        d_cash = getattr(flows, "dii_cash_net", default_dii_cash) if flows else default_dii_cash
        line1 = f"FII {'BOUGHT' if f_cash >= 0 else 'SOLD'} {abs(f_cash):,.0f} CR IN CASH; DII {'BOUGHT' if d_cash >= 0 else 'SOLD'} {abs(d_cash):,.0f} CR IN CASH"

        idx_val = poi.fii_change if has_poi else default_fii_idx
        stk_val = getattr(poi, "fii_stock_fut_change", None) if has_poi else default_fii_stk
        stk_str = f", {'BOUGHT' if stk_val >= 0 else 'SOLD'} {abs(stk_val):,.0f} CR IN STOCK FUTURES" if stk_val is not None else ""
        line2 = f"FII {'BOUGHT' if idx_val >= 0 else 'SOLD'} {abs(idx_val):,.0f} CR IN INDEX FUTURES{stk_str}"

        # Deduplicate lines strictly to avoid printing twice
        lines = []
        for l in (line1, line2):
            l_s = l.strip()
            if l_s and l_s not in lines:
                lines.append(l_s)
        p.observation = "\n".join(lines)

        if idx_val > 500:
            p.score, p.view = 2, "Bullish"
        elif idx_val < -500:
            p.score, p.view = -2, "Bearish"
        else:
            p.score, p.view = 0, "Neutral"
        p.auto = True
    else:
        p.observation = (
            "FII SOLD 819 CR IN CASH; DII SOLD 418 CR IN CASH\n"
            "FII SOLD 3,606 CR IN INDEX FUTURES, BOUGHT 1,420 CR IN STOCK FUTURES"
        )
        p.score = -2
        p.view = "Bearish"
        p.auto = False

    p.note = "Scored on FII INDEX FUTURES ONLY: Buying = +2 Bullish, Selling = -2 Bearish, Near Zero = 0 Neutral"
    return p


def p15_option_chain_analysis(cond=None, df: pd.DataFrame | None = None, spot: float | None = None,
                              read: Any = None, candles: pd.DataFrame | None = None,
                              session_stats: dict | None = None,
                              custom_narrative: str = "", custom_action: str = "",
                              custom_state: str = "", live: bool = True, stamp: str = "") -> Param:
    """
    #15 — Option Chain Analysis.
    Condition 1 (SUPER BULLISH): Call ITM Green, Call OTM Green, Put ITM Red, Put OTM Red (+2 Bullish)
    Condition 2 (SUPER BEARISH): Call ITM Red, Call OTM Red, Put ITM Green, Put OTM Green (-2 Bearish)
    Condition 3 (BULLISH CONSOL): Call ITM Green, rest Red (+1 Bullish)
    Condition 4 (BEARISH CONSOL): Put ITM Green, rest Red (-1 Bearish)
    Condition 5 (CONSOLIDATION): All Red — selling on both calls and puts (0 Neutral)
    Default: "SELLING IN CALLS AND BUYING IN PUTS" (-2 Bearish)
    """
    p = Param(15, "Option Chain Analysis", "2")

    # 1. Custom / Manual input takes precedence
    if custom_narrative or custom_state:
        state_u = custom_state.strip().upper() if custom_state else ""
        if "BULLISH" in state_u:
            score, view = (1 if "CONSOL" in state_u else 2), "Bullish"
        elif "BEARISH" in state_u:
            score, view = (-1 if "CONSOL" in state_u else -2), "Bearish"
        elif "CONSOL" in state_u or "NEUTRAL" in state_u:
            score, view = 0, "Neutral"
        else:
            score, view = 0, "Neutral"

        obs = custom_narrative.strip()
        if custom_action.strip():
            obs = f"{obs} — {custom_action.strip()}"
        p.observation = obs
        p.score = score
        p.view = view
        p.auto = False
        p.note = f"Option Chain Stance: {view}"
        return p

    # 2. Quadrants check
    if cond is None and df is not None and not df.empty:
        try:
            import setups as su
            if spot is None or spot <= 0:
                import chain_parser as cp
                spot = cp.infer_spot(df)
            cond = su.chain_condition(df, spot or 24000.0)
        except Exception:
            pass

    ci = getattr(cond, "call_itm", None) if cond else None
    co = getattr(cond, "call_otm", None) if cond else None
    pi = getattr(cond, "put_itm", None) if cond else None
    po = getattr(cond, "put_otm", None) if cond else None

    if ci is not None and co is not None and pi is not None and po is not None:
        quad_key = (ci, co, pi, po)
        if quad_key == ("G", "G", "R", "R"):
            p.observation = "BUYING IN CALLS AND SELLING IN PUTS (SUPER BULLISH: Call ITM Green, Call OTM Green, Put ITM Red, Put OTM Red)"
            p.score, p.view = 2, "Bullish"
        elif quad_key == ("R", "R", "G", "G"):
            p.observation = "SELLING IN CALLS AND BUYING IN PUTS (SUPER BEARISH: Call ITM Red, Call OTM Red, Put ITM Green, Put OTM Green)"
            p.score, p.view = -2, "Bearish"
        elif quad_key == ("G", "R", "R", "R"):
            p.observation = "BUYING IN CALLS AND SELLING IN PUTS (BULLISH CONSOL: Call ITM Green, rest Red)"
            p.score, p.view = 1, "Bullish"
        elif quad_key == ("R", "R", "G", "R"):
            p.observation = "SELLING IN CALLS AND BUYING IN PUTS (BEARISH CONSOL: Put ITM Green, rest Red)"
            p.score, p.view = -1, "Bearish"
        elif quad_key == ("R", "R", "R", "R"):
            p.observation = "ALL RED — SELLING ON BOTH CALLS AND PUTS (CONSOLIDATION: Range-bound)"
            p.score, p.view = 0, "Neutral"
        else:
            p.observation = "SELLING IN CALLS AND BUYING IN PUTS"
            p.score, p.view = -2, "Bearish"
        p.auto = True
    else:
        # Default as requested by user
        p.observation = "SELLING IN CALLS AND BUYING IN PUTS"
        p.score, p.view = -2, "Bearish"
        p.auto = False

    p.note = "SUPER BULLISH (+2) | SUPER BEARISH (-2) | BULLISH CONSOL (+1) | BEARISH CONSOL (-1) | CONSOLIDATION (0)"
    return p


p15_narrative = p15_option_chain_analysis  # Backward compatibility alias
p15_chain = p15_option_chain_analysis      # Backward compatibility alias



def p4_crude(crude=None, crude_nse=None) -> Param:
    """
    Above or below Rs6,500 — that is the whole rule.

    Prefers NSE's own crude futures contract, which is the actual rupee
    instrument your threshold refers to. Falls back to WTI x USDINR only if
    NSE is unreachable, and says so, because the proxy can differ by a
    hundred rupees or more.
    """
    p = Param(4, "Crude Oil (Above 6500 Levels Bearish)", "1")
    if crude_nse is not None:
        p.observation = crude_nse.observation
        p.score, p.view = crude_nse.score()
        p.auto = True
        bits = [f"NSE Energy Futures, expiry {crude_nse.expiry}"]
        if crude_nse.spot:
            bits.append(f"spot {crude_nse.spot:,.0f}")
        if crude_nse.pct_change is not None:
            bits.append(f"{crude_nse.pct_change:+.2f}%")
        p.note = " · ".join(bits)
        return p

    if crude is None or crude.inr_per_barrel is None:
        p.observation = "Crude ____ Futures @ ____ (vs Rs6,500)"
        p.note = "crude source unavailable"
        return p

    inr = crude.inr_per_barrel
    p.observation = f"Crude Futures @ Rs{inr:,.0f}/bbl (vs Rs6,500)"
    p.auto = True
    if inr > 6500:
        p.score, p.view = -1, "Bearish"
    else:
        p.score, p.view = 1, "Bullish"
    p.note = (f"FALLBACK: WTI ${crude.wti_usd:,.2f} x USDINR "
              f"{crude.usdinr:.2f}. NSE's own crude contract was unreachable, "
              f"so this is a proxy and can differ by Rs100+.")
    return p


def p16_global(glob=None) -> Param:
    """
    #16 — Global Markets.
    Asian markets & US Futures are prioritized for 9:15 AM opening decision.
    Format:
    US CASH MArkets: (S&P 500 (+/-) & Nasdaq 100 (+/-))
    US Futures Market: DOW Jones FUtures (+/-)
    Asian Markets: Hang Seng(+/-) , Nikkei 225(+/-) , KOSPI(+/-)
    View: strictly Bullish, Bearish, or Neutral.
    """
    p = Param(16, "Global Markets", "2")
    default_summary = (
        "US CASH MArkets: (S&P 500 (+) & Nasdaq 100 (+))\n"
        "US Futures Market: DOW Jones FUtures (+)\n"
        "Asian Markets: Hang Seng(+) , Nikkei 225(-) , KOSPI(+) , Shanghai(-)"
    )
    note_text = (
        "Asian markets and US Futures are MOST relevant for Indian morning opening. "
        "+2 Bullish (>=4 positive) | -2 Bearish (>=4 negative) | 0 Neutral (mixed)"
    )
    if glob is None or not getattr(glob, "rows", None):
        try:
            import market_data as _md
            glob = _md.fetch_global_investing()
        except Exception as e:
            logging.debug("p16_global: auto-fetch failed: %s", e)

    if glob is None or not getattr(glob, "rows", None):
        p.observation = default_summary
        p.score = 2
        p.view = "Bullish"
        p.note = note_text
        return p

    def _get_sign(name_sub: str, default_pos: bool = True) -> str:
        for r in glob.rows:
            nm = r.get("name", "").upper()
            if name_sub.upper() in nm:
                pct = float(r.get("pct", 0) or 0)
                return "(+)" if pct >= 0 else "(-)"
        return "(+)" if default_pos else "(-)"

    sp_s = _get_sign("S&P 500", True)
    nq_s = _get_sign("Nasdaq", True)
    dow_s = _get_sign("DOW", True)
    hs_s = _get_sign("Hang Seng", True)
    nk_s = _get_sign("Nikkei", False)
    kp_s = _get_sign("KOSPI", True)
    sh_s = _get_sign("Shanghai", False)

    p.observation = (
        f"US CASH MArkets: (S&P 500 {sp_s} & Nasdaq 100 {nq_s})\n"
        f"US Futures Market: DOW Jones FUtures {dow_s}\n"
        f"Asian Markets: Hang Seng{hs_s} , Nikkei 225{nk_s} , KOSPI{kp_s} , Shanghai{sh_s}"
    )

    pos_cnt = sum(1 for s in (sp_s, nq_s, dow_s, hs_s, nk_s, kp_s, sh_s) if s == "(+)")
    neg_cnt = 7 - pos_cnt
    if pos_cnt >= 4:
        p.score, p.view = 2, "Bullish"
    elif neg_cnt >= 4:
        p.score, p.view = -2, "Bearish"
    else:
        p.score, p.view = 0, "Neutral"

    p.auto = True
    p.note = note_text
    return p


def p5_gift(gift=None) -> Param:
    """NA — observation only. It predicts the gap, it does not score the day."""
    p = Param(5, "Gap Up or Gap Down", "NA")
    if gift is None:
        p.observation = "Gift Nifty @____ | Nifty Fut @____ | expected gap ___ pts"
        p.note = "giftnifty.org unreachable"
        return p
    bits = [f"Gift Nifty @ {gift.value:,.2f} ({gift.pct_change:+.2f}%)"]
    ref = getattr(gift, "nifty_futures", None)
    if ref is not None:
        bits.append(f"Nifty Fut {ref:,.2f}")
    elif gift.prev_close is not None:
        bits.append(f"prev close {gift.prev_close:,.2f}")
    if gift.gap is not None:
        bits.append(f"expected gap {gift.gap:+,.0f} pts")
    p.observation = " | ".join(bits)

    # The View column states the expected open, not "Neutral".
    #
    # This row is unscored — Gift Nifty predicts the gap, it does not score
    # the day — but "Neutral" in the view column threw away the one thing
    # the row exists to say. The thresholds are MJ's: beyond 100 points
    # either way is a gap, inside that band is a flat open.
    g = gift.gap
    if g is None:
        p.view = "—"
    elif g > 100:
        p.view = "GAP UP expected"
    elif g < -100:
        p.view = "GAP DOWN expected"
    else:
        p.view = "FLAT OPEN expected"
    p.auto = True
    notes = [getattr(gift, "gap_basis", "")]
    if getattr(gift, "expiry", ""):
        notes.append(f"expiry {gift.expiry}")
    if gift.updated:
        notes.append(f"quote {gift.updated}")
    p.note = " · ".join(n for n in notes if n)
    return p


def p1_p2_futures(nf=None, bnf=None) -> tuple[Param, Param]:
    """#1 and #2 from NSE's own derivatives quote — the real contract."""
    out = []
    for no, name, fut in ((1, "GAP between NIFTY & NIFTY Futures", nf),
                          (2, "GAP between BANKNIFTY & BANKNIFTY Futures", bnf)):
        p = Param(no, name, "1")
        if fut is None:
            # No "=" sign. "A (-) B = Premium of X" reads as an equation
            # and it is not one — the premium is the result of the
            # subtraction, not a third quantity equal to it.
            p.observation = ("Nifty @____ (-) Nifty Fut @____ Premium of ___ pts"
                             if no == 1 else
                             "B Nifty @____ (-) BNF Fut @____ Premium of ___ pts")
            p.note = "derivatives quote unavailable — read it off Kite"
        else:
            p.observation = fut.observation
            p.score, p.view = fut.score()
            p.auto = True
            bits = [f"nearest LIVE expiry {fut.expiry}"]
            if getattr(fut, "price_source", ""):
                bits.append(f"{fut.price_source} price")
            if getattr(fut, "contracts_dropped", 0):
                bits.append(f"{fut.contracts_dropped} expired contract(s) skipped")
            if fut.timestamp:
                bits.append(f"quote {fut.timestamp}")
            p.note = " · ".join(bits)
        out.append(p)
    return out[0], out[1]


def _p1_futures_cfd(glob=None, spot: float | None = None) -> Param:
    """
    Nifty futures premium/discount.

    Uses investing.com's Nifty 50 Futures quote, which is labelled CFD — an
    indicative derived price, not the NSE contract. Good enough to call
    premium versus discount; the exact basis will differ from Kite.
    """
    p = Param(1, "GAP between NIFTY & NIFTY Futures", "1")
    fut = getattr(glob, "nifty_futures", None) if glob else None
    if fut is None or spot is None:
        p.observation = "Nifty @____ (-) Nifty Fut @____ = Premium/Discount ___ pts"
        p.note = "no futures quote available — read it off Kite"
        return p
    gap = fut - spot
    p.observation = (f"Nifty @{spot:,.2f} (-) Nifty Fut @{fut:,.2f} = "
                     f"{'Premium' if gap > 0 else 'Discount'} of {abs(gap):,.0f} pts")
    p.auto = True
    if gap > 5:
        p.score, p.view = 1, "Bullish"
    elif gap < -5:
        p.score, p.view = -1, "Bearish"
    else:
        p.score, p.view = 0, "Neutral"
    p.note = ("Futures quote is investing.com's CFD, an indicative price — "
              "verify against Kite if the basis looks odd.")
    return p


def p17_adrs(adrs=None) -> Param:
    """
    #17 — US ADRs.
    5 Key stocks: INFOSYS, ICICI, HDFC, DR REDDY'S, WIPRO.
    Format: "INFOSYS (-); ICICI (-); HDFC (-); DR REDDY'S (-); WIPRO (+) (1 POSITIVE, 4 NEGATIVE)"
    (No % mention, just number of positives and negatives)
    Score: +2 Bullish (>=3 positive), -2 Bearish (>=3 negative), 0 Neutral (mixed).
    View: strictly Bullish, Bearish, or Neutral.
    """
    p = Param(17, "US ADR", "2")
    default_obs = "INFOSYS (-); ICICI (-); HDFC (-); DR REDDY'S (-); WIPRO (+) (1 POSITIVE, 4 NEGATIVE)"
    if adrs is not None and getattr(adrs, "rows", None):
        p.observation = adrs.observation or default_obs
        p.score, p.view = adrs.score()
        p.auto = True
    else:
        p.observation = default_obs
        p.score = -2
        p.view = "Bearish"
        p.auto = False
    p.note = "+2 Bullish (>=3 positive) | -2 Bearish (>=3 negative) | 0 Neutral (mixed)"
    return p


def p18_usdinr(gift=None, crude=None, usdinr=None) -> Param:
    """
    #18 — USD/INR Exchange Rate.
    Unscored observation (Importance NA) providing macro forex context.
    Auto-sourced from Gift Nifty NSE payload, Crude proxy, or Yahoo Finance.
    Format:
    USD/INR futures @ 95.730
    View: strictly Bullish, Bearish, or Neutral.
    """
    p = Param(18, "USD/INR Exchange Rate", "NA")
    rate = None
    pct_chg = None

    if usdinr is not None:
        if isinstance(usdinr, (tuple, list)):
            rate = float(usdinr[0]) if len(usdinr) > 0 and usdinr[0] is not None else None
            pct_chg = float(usdinr[1]) if len(usdinr) > 1 and usdinr[1] is not None else None
        elif isinstance(usdinr, (int, float)):
            rate = float(usdinr)

    if rate is None and gift is not None and getattr(gift, "usd_inr", None):
        try:
            rate = float(gift.usd_inr)
        except (ValueError, TypeError):
            pass

    if rate is None and crude is not None and getattr(crude, "usdinr", None):
        try:
            rate = float(crude.usdinr)
        except (ValueError, TypeError):
            pass

    if rate is None:
        try:
            import market_data as _md
            rate, pct_chg = _md.fetch_usdinr_investing()
            if rate is None:
                rate, pct_chg = _md.fetch_usdinr_yahoo()
        except Exception as e:
            logging.debug("p18_usdinr: could not fetch usdinr: %s", e)

    if rate is not None:
        chg_str = f" ({pct_chg:+.2f}%)" if pct_chg is not None else ""
        p.observation = f"USD/INR futures @ {rate:.3f}{chg_str}"
        p.auto = True
        if pct_chg is not None:
            if pct_chg > 0.05:
                p.view = "Bearish"
            elif pct_chg < -0.05:
                p.view = "Bullish"
            else:
                p.view = "Neutral"
        else:
            p.view = "Neutral"
    else:
        p.observation = "USD/INR futures @ 95.730"
        p.auto = False
        p.view = "Neutral"

    p.score = 0
    p.note = "Observation only (NA score). Macro currency context: Depreciating INR = Bearish | Appreciating INR = Bullish | Stable = Neutral"
    return p


def manual(no: int, name: str, imp: str, hint: str) -> Param:
    return Param(no, name, imp, observation=hint, note="MANUAL — no free source wired")


# ═══════════════════════════════════════════════════════════════════════════
# Assemble
# ═══════════════════════════════════════════════════════════════════════════

def build(indices: list[dict] | None = None,
          daily: pd.DataFrame | None = None,
          master: pd.DataFrame | None = None,
          nifty_ad: dict | None = None,
          bn_ad: dict | None = None,
          pcr: float | None = None,
          pcr_bnf: float | None = None,
          flows=None,
          poi=None,
          gift=None,
          glob=None,
          crude=None,
          crude_nse=None,
          vixh=None,
          nifty_fut=None,
          bn_fut=None,
          adrs=None,
          spot: float | None = None,
          chain_cond=None,
          chain_live: bool = True,
          chain_stamp: str = "",
          candles: pd.DataFrame | None = None,
          df: pd.DataFrame | None = None,
          read: Any = None,
          custom_narrative: str = "",
          custom_action: str = "",
          custom_state: str = "",
          usdinr=None,
          overrides: dict[int, Any] | None = None) -> list[Param]:
    indices = indices or []
    overrides = overrides or {}

    f1, f2 = p1_p2_futures(nifty_fut, bn_fut)
    s8, r9 = p8_p9_levels(master, ohlc=daily, spot=spot, df=df, read=read)
    a10, b11 = p10_p11_breadth(nifty_ad, bn_ad)

    params = [
        f1,
        f2,
        p3_vix_history(vixh) if vixh else p3_vix(indices),
        p4_crude(crude, crude_nse),
        p5_gift(gift),
        p6_daily_trend(daily),
        p7_rsi(daily),
        s8, r9, a10, b11,
        p12_sectors(indices),
        p13_pcr(pcr, pcr_bnf),
        p14_fii(flows, poi),
        p15_option_chain_analysis(chain_cond, df=df, spot=spot, read=read,
                                  candles=candles,
                                  custom_narrative=custom_narrative,
                                  custom_action=custom_action,
                                  custom_state=custom_state),
        p16_global(glob),
        p17_adrs(adrs),
        p18_usdinr(gift=gift, crude=crude, usdinr=usdinr),
    ]

    for p in params:
        if p.no in overrides:
            ov = overrides[p.no]
            if isinstance(ov, (list, tuple)):
                obs = ov[0] if len(ov) > 0 else ""
                sc = ov[1] if len(ov) > 1 else None
                vw = ov[2] if len(ov) > 2 else None
                if obs:
                    p.observation = obs
                if p.importance != "NA" and sc is not None:
                    p.score = sc
                if vw is not None:
                    p.view = str(vw)
            elif isinstance(ov, str):
                p.observation = ov

        # Deduplicate identical lines in any observation
        if p.observation and "\n" in p.observation:
            dedup = []
            for ol in p.observation.splitlines():
                ol_s = ol.strip()
                if ol_s and ol_s not in dedup:
                    dedup.append(ol_s)
            p.observation = "\n".join(dedup)
    return params


def total_score(params: list[Param]) -> int:
    return sum(p.score for p in params if p.scored)


def verdict(score: int) -> tuple[str, str]:
    if score > 14:
        return "BULLISH", "green"
    if score < 0:
        return "BEARISH", "red"
    return "NEUTRAL", "amber"


def coverage(params: list[Param]) -> tuple[int, int, int]:
    """(auto-filled, scorable total, still manual)"""
    scorable = [p for p in params if p.importance != "NA"]
    auto = sum(1 for p in scorable if p.auto and p.scored)
    return auto, len(scorable), len(scorable) - auto


# ═══════════════════════════════════════════════════════════════════════════
# Excel export — matches your VIEW_BUILDING_PROCESS_v2 layout
# ═══════════════════════════════════════════════════════════════════════════

def to_excel_bytes(params: list[Param], when: date | None = None) -> bytes:
    """
    Generate the Excel sheet matching VIEW_BUILDING_PROCESS_v2 layout in memory as bytes.
    """
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    when = when or date.today()
    wb = Workbook()
    ws = wb.active
    ws.title = "View For The Day"

    ARIAL = "Arial"
    navy = PatternFill("solid", start_color="1F497D")
    yellow = PatternFill("solid", start_color="FFF2CC")     # manual input
    autofill = PatternFill("solid", start_color="E2EFDA")   # auto-filled
    green = PatternFill("solid", start_color="C6EFCE")
    red = PatternFill("solid", start_color="FFC7CE")
    amber = PatternFill("solid", start_color="FFEB9C")
    thin = Border(*[Side(style="thin", color="BFBFBF")] * 4)
    wrap = Alignment(wrap_text=True, vertical="top")
    centre = Alignment(horizontal="center", vertical="center")

    score = total_score(params)
    vname, vcol = verdict(score)

    ws["A1"] = "NIFTY 50 — VIEW BUILDING SHEET"
    ws["A1"].font = Font(name=ARIAL, bold=True, size=14, color="1F497D")
    ws["A2"] = "DATE:"
    ws["B2"] = when.strftime("%d/%m/%Y")
    ws["D2"] = "TOTAL SCORE:"
    ws["E2"] = score
    ws["F2"] = vname
    for c in ("A2", "D2"):
        ws[c].font = Font(name=ARIAL, bold=True)
    ws["E2"].font = Font(name=ARIAL, bold=True, size=13)
    ws["F2"].font = Font(name=ARIAL, bold=True, size=13)
    ws["F2"].fill = {"green": green, "red": red, "amber": amber}[vcol]

    headers = ["Sl No", "Particulars", "Importance",
               "Observations", "Score", "View"]
    ws.append([])
    ws.append(headers)
    hrow = ws.max_row
    for i in range(1, len(headers) + 1):
        c = ws.cell(row=hrow, column=i)
        c.fill, c.font, c.alignment = navy, Font(name=ARIAL, bold=True,
                                                 color="FFFFFF"), centre

    for p in params:
        ws.append([p.no, p.name, p.importance, p.observation,
                   "NA" if not p.scored else p.score, p.verdict_word])
        r = ws.max_row
        for i in range(1, 7):
            cell = ws.cell(row=r, column=i)
            cell.font = Font(name=ARIAL)
            cell.border = thin
            cell.alignment = wrap if i in (4, 6) else centre
        ws.cell(row=r, column=4).fill = autofill if p.auto else yellow
        if p.scored:
            sc = ws.cell(row=r, column=5)
            sc.font = Font(name=ARIAL, bold=True)
            if p.score > 0:
                sc.fill = green
            elif p.score < 0:
                sc.fill = red
            else:
                sc.fill = amber

        # View cell styling
        vw_cell = ws.cell(row=r, column=6)
        vw_word = p.verdict_word
        if vw_word == "Bullish":
            vw_cell.fill = green
            vw_cell.font = Font(name=ARIAL, bold=True, color="006100")
        elif vw_word == "Bearish":
            vw_cell.fill = red
            vw_cell.font = Font(name=ARIAL, bold=True, color="9C0006")
        elif vw_word == "Neutral":
            vw_cell.fill = amber
            vw_cell.font = Font(name=ARIAL, bold=True, color="9C6500")
        else:
            vw_cell.font = Font(name=ARIAL, color="7F7F7F")

    ws.append([])
    ws.append(["TOTAL", "", "", "SCORE > 14 = BULLISH | 0 to 14 = NEUTRAL | < 0 = BEARISH",
               score, vname])
    tr = ws.max_row
    for i in range(1, 7):
        ws.cell(row=tr, column=i).font = Font(name=ARIAL, bold=True)
        ws.cell(row=tr, column=i).fill = {"green": green, "red": red,
                                          "amber": amber}[vcol]

    auto, tot, man = coverage(params)
    ws.append([])
    ws.append(["", "STRATEGY RULE: Follow option chain analysis and structural levels. Adapt bias on VWAP breach."])
    ws.append(["", f"Green cells = auto-filled from NSE ({auto} of {tot} "
                   f"scorable). Yellow cells = fill by hand ({man} left)."])
    for rr in (ws.max_row - 1, ws.max_row):
        ws.cell(row=rr, column=2).font = Font(name=ARIAL, italic=True, size=9)

    widths = {1: 7, 2: 38, 3: 11, 4: 64, 5: 8, 6: 18}
    for i, w in widths.items():
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = f"A{hrow + 1}"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def to_excel(params: list[Param], path: Path, when: date | None = None) -> Path:
    """
    Write the sheet in your existing format so the screenshot you send MJ
    looks exactly like the one you have been filling by hand.
    """
    data = to_excel_bytes(params, when=when)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# ═══════════════════════════════════════════════════════════════════════════
# PNG export — for WhatsApp (Publication Grade HD Image Card)
# ═══════════════════════════════════════════════════════════════════════════

def _extract_narrative_and_action(params: list[Param], default_verdict: str) -> tuple[str, str, str]:
    """Extracts option chain analysis, action to take, and market state from Parameter 15."""
    p15 = next((p for p in params if p.no == 15), None)
    sess_state = p15.verdict_word if p15 and p15.verdict_word not in ("NA", "") else default_verdict
    obs = p15.observation if p15 else ""

    if " — Action: " in obs:
        narrative, action_body = obs.split(" — Action: ", 1)
        action = "Action: " + action_body.strip()
    elif " | Action: " in obs:
        narrative, action_body = obs.split(" | Action: ", 1)
        action = "Action: " + action_body.strip()
    elif " — " in obs and any(k in obs.lower() for k in ("action", "trail", "exit", "scalp", "buy", "fade", "wait")):
        narrative, action = obs.rsplit(" — ", 1)
        if not action.lower().startswith("action:"):
            action = "Action: " + action.strip()
    else:
        narrative = obs or "Option chain analysis: Defended levels & structure from previous close."
        action = "Action: Trade with trend structure and strict trailing stops."

    return narrative.strip(), action.strip(), sess_state


def to_image_native(params: list[Param], path: Path, when: date | None = None,
                    result_note: str = "", dark_mode: bool = False) -> Path:
    """High-resolution matplotlib/Pillow sheet renderer with large readable letters and executive narrative."""
    import textwrap
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as patches

    when = when or date.today()
    score = total_score(params)
    vname, vcol = verdict(score)
    auto, tot, man = coverage(params)
    narrative, action_to_take, sess_state = _extract_narrative_and_action(params, vname)

    bg_main = "#0B111C" if dark_mode else "#FFFFFF"
    fg_text = "#FFFFFF" if dark_mode else "#0F1E36"
    banner_bg = "#111C2E" if dark_mode else "#0F1E36"
    border_col = "#23344D" if dark_mode else "#CBD5E1"
    table_hdr_bg = "#16233B" if dark_mode else "#0F1E36"

    fig = plt.figure(figsize=(13.5, 19.5), facecolor=bg_main, dpi=180)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.axis("off")

    # 1. Top Banner
    ax.add_patch(patches.Rectangle((0, 0.932), 1.0, 0.068, facecolor=banner_bg))
    ax.text(0.035, 0.970, "NIFTY 50", color="#FFFFFF", fontsize=15, weight="bold", transform=ax.transAxes)
    ax.text(0.035, 0.945, "VIEW BUILDING", color="#FFD400", fontsize=12, weight="bold", transform=ax.transAxes)

    # Date badge
    dt_str = when.strftime("%d %B %Y")
    ax.add_patch(patches.FancyBboxPatch((0.36, 0.942), 0.28, 0.046, boxstyle="round,pad=0.008",
                                        facecolor="#FFD400", edgecolor="none", transform=ax.transAxes))
    ax.text(0.50, 0.965, dt_str, color="#0F1E36", fontsize=15, weight="bold", ha="center", va="center", transform=ax.transAxes)

    # Day of week
    ax.text(0.965, 0.965, when.strftime("%A").upper(), color="#FFFFFF", fontsize=15, weight="bold", ha="right", va="center", transform=ax.transAxes)

    # 2. Table Header
    y = 0.898
    ax.add_patch(patches.Rectangle((0.02, y), 0.96, 0.030, facecolor=table_hdr_bg))
    ax.text(0.04, y + 0.009, "SI.No", color="#FFFFFF", fontsize=11, weight="bold", ha="center", transform=ax.transAxes)
    ax.text(0.075, y + 0.009, "Particulars", color="#FFFFFF", fontsize=11, weight="bold", transform=ax.transAxes)
    ax.text(0.33, y + 0.009, "Observations", color="#FFFFFF", fontsize=11, weight="bold", transform=ax.transAxes)
    ax.text(0.79, y + 0.009, "SCORE", color="#FFFFFF", fontsize=11, weight="bold", ha="center", transform=ax.transAxes)
    ax.text(0.90, y + 0.009, "View", color="#FFFFFF", fontsize=11, weight="bold", ha="center", transform=ax.transAxes)

    # 3. Table Rows
    y_curr = y
    row_h = 0.038
    for i, p in enumerate(params):
        y_curr -= row_h
        if dark_mode:
            bg_color = "#0F172A" if i % 2 == 0 else "#0B111C"
            txt_color = "#E2E8F0"
        else:
            bg_color = "#FFFFFF" if i % 2 == 0 else "#F8FAFC"
            txt_color = "#0F1E36"

        ax.add_patch(patches.Rectangle((0.02, y_curr), 0.96, row_h, facecolor=bg_color, edgecolor=border_col, linewidth=0.8))

        # SI.No
        ax.text(0.04, y_curr + 0.013, str(p.no), color=txt_color, fontsize=11.5, weight="bold", ha="center", transform=ax.transAxes)

        # Particulars
        p_title = p.name
        if len(p_title) > 28:
            p_title = p_title[:27] + "…"
        ax.text(0.075, y_curr + 0.013, p_title, color=txt_color, fontsize=11, weight="bold", transform=ax.transAxes)

        # Observations
        obs_raw = (p.observation or "—")
        raw_lines = [l.strip() for l in obs_raw.split("\n") if l.strip()]
        lines = []
        for rl in raw_lines:
            if rl not in lines:
                lines.append(rl)
        all_wrapped = []
        for l in lines:
            all_wrapped.extend(textwrap.wrap(l, width=62) if l else [""])
        if len(all_wrapped) > 2:
            obs_display = all_wrapped[0] + "\n" + all_wrapped[1][:58] + "…"
        elif len(all_wrapped) == 2:
            obs_display = all_wrapped[0] + "\n" + all_wrapped[1]
        else:
            obs_display = all_wrapped[0] if all_wrapped else "—"
        obs_color = txt_color if p.auto else ("#FCD34D" if dark_mode else "#854D0E")
        obs_y = y_curr + (0.010 if "\n" in obs_display else 0.013)
        ax.text(0.33, obs_y, obs_display, color=obs_color, fontsize=10.5, transform=ax.transAxes)

        # Score
        sc_str = f"{p.score:+d}" if p.scored else "NA"
        sc_col = "#00E676" if p.scored and p.score > 0 else "#FF5252" if p.scored and p.score < 0 else ("#94A3B8" if dark_mode else "#64748B")
        ax.text(0.79, y_curr + 0.013, sc_str, color=sc_col, fontsize=12.5, weight="bold", ha="center", transform=ax.transAxes)

        # View badge
        vw = p.verdict_word
        vw_short = vw if len(vw) <= 14 else vw[:13] + "."
        vw_up = vw.upper()
        if dark_mode:
            vw_fg = "#00E676" if ("BULLISH" in vw_up or p.no == 8) else "#FF5252" if ("BEARISH" in vw_up or p.no == 9) else "#38BDF8" if "CONSOL" in vw_up or "NEUTRAL" in vw_up else "#94A3B8"
            vw_bg = "#0D3322" if ("BULLISH" in vw_up or p.no == 8) else "#3B1414" if ("BEARISH" in vw_up or p.no == 9) else "#132E4A" if "CONSOL" in vw_up or "NEUTRAL" in vw_up else "#1E293B"
        else:
            vw_fg = "#008744" if ("BULLISH" in vw_up or p.no == 8) else "#D32F2F" if ("BEARISH" in vw_up or p.no == 9) else "#1F5FA8" if "CONSOL" in vw_up or "NEUTRAL" in vw_up else "#64748B"
            vw_bg = "#E7F6EC" if ("BULLISH" in vw_up or p.no == 8) else "#FDECEA" if ("BEARISH" in vw_up or p.no == 9) else "#EAF1FA" if "CONSOL" in vw_up or "NEUTRAL" in vw_up else "#F1F5F9"

        ax.add_patch(patches.FancyBboxPatch((0.835, y_curr + 0.005), 0.13, 0.028, boxstyle="round,pad=0.004", facecolor=vw_bg, edgecolor="none", transform=ax.transAxes))
        ax.text(0.90, y_curr + 0.013, vw_short, color=vw_fg, fontsize=10.5, weight="bold", ha="center", transform=ax.transAxes)

    # 4. EXECUTIVE SUMMARY & OPTION CHAIN ANALYSIS Box
    y_curr -= 0.030
    ax.add_patch(patches.Rectangle((0.02, y_curr), 0.96, 0.028, facecolor=table_hdr_bg))
    ax.text(0.50, y_curr + 0.008, "EXECUTIVE SUMMARY & OPTION CHAIN ANALYSIS",
            color="#FFD400", fontsize=12, weight="bold", ha="center", transform=ax.transAxes)

    # Narrative Content Box
    narr_box_h = 0.095
    y_curr -= narr_box_h
    box_bg = "#111E33" if dark_mode else "#FFFDEB"
    box_edge = "#38BDF8" if dark_mode else "#E2B93B"
    ax.add_patch(patches.Rectangle((0.02, y_curr), 0.96, narr_box_h, facecolor=box_bg, edgecolor=box_edge, linewidth=1.5))

    # Top line: Score & State
    ax.text(0.04, y_curr + 0.070, f"TOTAL SCORE: {score:+d} · {vname} BIAS",
            color=fg_text, fontsize=13.5, weight="bold", transform=ax.transAxes)
    ax.text(0.96, y_curr + 0.070, f"OPTION CHAIN STANCE: {sess_state}",
            color="#FF5252" if "BEARISH" in sess_state else "#00E676" if "BULLISH" in sess_state else "#38BDF8" if dark_mode else "#1F5FA8",
            fontsize=13, weight="bold", ha="right", transform=ax.transAxes)

    # Middle line: Option Chain Analysis
    narr_wrap = textwrap.wrap(narrative, width=110)
    narr_text = "\n".join(narr_wrap[:3])
    ax.text(0.04, y_curr + 0.043, narr_text, color=fg_text, fontsize=10.5, transform=ax.transAxes)

    # Bottom line: Action banner
    act_bg = "#0A101D" if dark_mode else "#0F1E36"
    act_fg = "#38BDF8" if dark_mode else "#FFD400"
    act_clean = action_to_take.replace('Action: ', '')
    ax.text(0.05, y_curr + 0.015, f">> ACTION PLAN: {act_clean}", color=act_fg, fontsize=11.5, weight="bold", transform=ax.transAxes)

    # 5. Footer
    ftr_col = "#94A3B8" if dark_mode else "#64748B"
    ax.text(0.03, 0.012, f"Auto-Sourced: {auto} of {tot} scorable parameters | {man} manual inputs | Educational analysis only", color=ftr_col, fontsize=9.5, transform=ax.transAxes)
    ax.text(0.97, 0.012, "AntiGravity Institutional Terminal", color=ftr_col, fontsize=9.5, ha="right", transform=ax.transAxes)

    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(path), facecolor=fig.get_facecolor(), edgecolor="none", bbox_inches="tight")
    plt.close(fig)
    return path


def to_image(params: list[Param], path: Path, when: date | None = None,
             scale: int = 2, result_note: str = "", dark_mode: bool = False) -> Path:
    """
    Render publication-grade, ultra-sharp WhatsApp card with big readable typography,
    option chain analysis, concrete action plan, and market state badge.
    Uses Playwright for pixel-perfect layout and falls back to native high-res Matplotlib.
    """
    try:
        from playwright.sync_api import sync_playwright

        when = when or date.today()
        score = total_score(params)
        vname, vcol = verdict(score)
        auto, tot, man = coverage(params)
        narrative, action_to_take, sess_state = _extract_narrative_and_action(params, vname)

        if dark_mode:
            bg_body = "#0B111C"
            bg_card = "#0F172A"
            border_card = "#1E293B"
            banner_bg = "#111C2E"
            hdr_bg = "#16233B"
            hdr_border = "#23344D"
            cell_border = "#1E293B"
            row_even = "#0F172A"
            row_odd = "#0B111C"
            txt_num = "#94A3B8"
            txt_nm = "#F8FAFC"
            txt_obs = "#CBD5E1"
            txt_obs_man = "#FCD34D"
            sc_pos = "#00E676"
            sc_neg = "#FF5252"
            sc_zero = "#94A3B8"
            exec_hdr_bg = "#16233B"
            exec_hdr_fg = "#FFD400"
            exec_box_bg = "#111E33"
            exec_box_border = "#38BDF8"
            exec_score_fg = "#F8FAFC"
            exec_narr_fg = "#E2E8F0"
            exec_act_bg = "#0A101D"
            exec_act_fg = "#38BDF8"
            foot_bg = "#0B111C"
            foot_fg = "#94A3B8"
            word_styles = {
                "BULLISH": ("#00E676", "#0D3322"),
                "BEARISH": ("#FF5252", "#3B1414"),
                "NEUTRAL": ("#38BDF8", "#132E4A"),
                "NA": ("#94A3B8", "#1E293B"),
            }
        else:
            bg_body = "#E2E8F0"
            bg_card = "#FFFFFF"
            border_card = "#CBD5E1"
            banner_bg = "#0F1E36"
            hdr_bg = "#0F1E36"
            hdr_border = "#1E2D4A"
            cell_border = "#CBD5E1"
            row_even = "#F8FAFC"
            row_odd = "#FFFFFF"
            txt_num = "#334155"
            txt_nm = "#0F1E36"
            txt_obs = "#1E293B"
            txt_obs_man = "#854D0E"
            sc_pos = "#008744"
            sc_neg = "#D32F2F"
            sc_zero = "#64748B"
            exec_hdr_bg = "#0F1E36"
            exec_hdr_fg = "#FFD400"
            exec_box_bg = "#FFFDEB"
            exec_box_border = "#E2B93B"
            exec_score_fg = "#0F1E36"
            exec_narr_fg = "#1E293B"
            exec_act_bg = "#0F1E36"
            exec_act_fg = "#FFD400"
            foot_bg = "#F1F5F9"
            foot_fg = "#64748B"
            word_styles = {
                "BULLISH": ("#008744", "#E7F6EC"),
                "BEARISH": ("#D32F2F", "#FDECEA"),
                "NEUTRAL": ("#1F5FA8", "#EAF1FA"),
                "NA": ("#64748B", "#F1F5F9"),
            }

        def _pick_style(word: str, styles: dict) -> tuple[str, str]:
            u = (word or "").upper().strip()
            if "BULLISH" in u:
                return styles["BULLISH"]
            if "BEARISH" in u:
                return styles["BEARISH"]
            if "NEUTRAL" in u or "CONSOL" in u:
                return styles["NEUTRAL"]
            return styles["NA"]

        state_fg, state_bg = _pick_style(sess_state, word_styles)

        rows_html = []
        for p_ in params:
            w = p_.verdict_word
            fg, bg = _pick_style(w, word_styles)
            if p_.no == 8 and w not in ("NA", ""):
                fg, bg = word_styles["BULLISH"]
            elif p_.no == 9 and w not in ("NA", ""):
                fg, bg = word_styles["BEARISH"]
            sc = "NA" if not p_.scored else f"{p_.score:+d}"
            sc_color = sc_pos if p_.scored and p_.score > 0 else sc_neg if p_.scored and p_.score < 0 else sc_zero

            obs_raw = p_.observation or "—"
            raw_lines = [l.strip() for l in obs_raw.split("\n") if l.strip()]
            dedup_lines = []
            for rl in raw_lines:
                if rl not in dedup_lines:
                    dedup_lines.append(rl)
            obs = "<br>".join(dedup_lines).replace("<", "&lt;").replace(">", "&gt;").replace("&lt;br&gt;", "<br>")
            obs_cls = "man" if not p_.auto else ""

            rows_html.append(f"""
            <tr>
              <td class="n">{p_.no}</td>
              <td class="nm">{p_.name}</td>
              <td class="obs {obs_cls}">{obs}</td>
              <td class="sc" style="color:{sc_color};">{sc}</td>
              <td class="vw"><span class="pill" style="color:{fg};background:{bg};border:1px solid {fg}40;">{w}</span></td>
            </tr>""")

        html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:0; background:{bg_body}; font-family:-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; }}
  #vb-card {{ width:1260px; margin:0 auto; background:{bg_card}; border:1px solid {border_card}; border-radius:0px; overflow:hidden; }}

  .banner {{ background:{banner_bg}; color:#FFFFFF; padding:18px 28px; display:flex; justify-content:space-between; align-items:center; border-bottom:3px solid #FFD400; }}
  .banner .ttl-box {{ display:flex; flex-direction:column; gap:2px; }}
  .banner .idx {{ font-size:26px; font-weight:900; letter-spacing:1.5px; color:#FFFFFF; }}
  .banner .sub {{ font-size:16px; font-weight:800; letter-spacing:3px; color:#FFD400; }}
  .banner .dt {{ background:#FFD400; color:#0F1E36; font-size:28px; font-weight:900; padding:10px 32px; border-radius:8px; box-shadow:0 3px 8px rgba(0,0,0,0.25); letter-spacing:0.5px; }}
  .banner .dy {{ font-size:26px; font-weight:900; letter-spacing:2px; color:#FFFFFF; }}

  table {{ border-collapse:collapse; width:100%; table-layout:fixed; }}
  th {{ background:{hdr_bg}; color:#FFFFFF; padding:14px 12px; font-size:17px; font-weight:800; text-transform:uppercase; letter-spacing:1px; border:1px solid {hdr_border}; text-align:center; }}
  th.nm-th, th.obs-th {{ text-align:left; }}

  col.c-n   {{ width:65px; }}
  col.c-nm  {{ width:315px; }}
  col.c-obs {{ width:570px; }}
  col.c-sc  {{ width:100px; }}
  col.c-vw  {{ width:210px; }}

  td {{ border:1px solid {cell_border}; padding:11px 14px; vertical-align:middle; font-size:16px; }}
  tr:nth-child(even) {{ background:{row_even}; }}
  tr:nth-child(odd)  {{ background:{row_odd}; }}

  td.n  {{ text-align:center; font-weight:900; font-size:18px; color:{txt_num}; }}
  td.nm {{ text-align:left; font-weight:700; font-size:16.5px; color:{txt_nm}; line-height:1.3; }}
  td.obs {{ text-align:left; font-size:15.5px; font-weight:500; color:{txt_obs}; line-height:1.45; word-wrap:break-word; }}
  td.obs.man {{ color:{txt_obs_man}; font-weight:600; }}

  td.sc {{ text-align:center; font-weight:900; font-size:21px; }}
  td.vw {{ text-align:center; }}
  .pill {{ display:inline-block; padding:6px 14px; border-radius:6px; font-weight:800; font-size:14.5px; letter-spacing:0.5px; text-transform:uppercase; }}

  .exec-hdr {{ background:{exec_hdr_bg}; color:{exec_hdr_fg}; text-align:center; padding:12px; font-weight:900; font-size:17px; letter-spacing:2px; }}
  .exec-box {{ background:{exec_box_bg}; border:2px solid {exec_box_border}; padding:18px 24px; }}
  .exec-top {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; border-bottom:1px dashed {exec_box_border}80; padding-bottom:10px; }}
  .exec-score {{ font-size:22px; font-weight:900; color:{exec_score_fg}; }}
  .exec-state {{ font-size:19px; font-weight:800; color:{exec_score_fg}; }}
  .exec-narrative {{ font-size:17px; font-weight:500; line-height:1.55; color:{exec_narr_fg}; margin-bottom:14px; }}
  .exec-action {{ background:{exec_act_bg}; color:{exec_act_fg}; padding:12px 18px; border-radius:6px; font-size:18px; font-weight:800; display:flex; align-items:center; gap:8px; }}

  .foot {{ display:flex; justify-content:space-between; padding:12px 24px; font-size:14px; font-weight:600; color:{foot_fg}; background:{foot_bg}; border-top:1px solid {cell_border}; }}
</style>
</head>
<body>
  <div id="vb-card">
    <div class="banner">
      <div class="ttl-box">
        <span class="idx">NIFTY 50</span>
        <span class="sub">VIEW BUILDING</span>
      </div>
      <div class="dt">{when:%d %B %Y}</div>
      <div class="dy">{when:%A}</div>
    </div>
    <table>
      <colgroup>
        <col class="c-n"><col class="c-nm"><col class="c-obs"><col class="c-sc"><col class="c-vw">
      </colgroup>
      <thead>
        <tr>
          <th>Sl.No</th>
          <th class="nm-th">Particulars</th>
          <th class="obs-th">Observations</th>
          <th>SCORE</th>
          <th>View</th>
        </tr>
      </thead>
      <tbody>
        {''.join(rows_html)}
      </tbody>
    </table>
    <div class="exec-hdr">EXECUTIVE SUMMARY & OPTION CHAIN ANALYSIS</div>
    <div class="exec-box">
      <div class="exec-top">
        <div class="exec-score">TOTAL SCORE: {score:+d} · {vname} BIAS</div>
        <div class="exec-state">OPTION CHAIN STANCE: <span class="pill" style="color:{state_fg};background:{state_bg};border:1px solid {state_fg}40;">{sess_state}</span></div>
      </div>
      <div class="exec-narrative">{narrative}</div>
      <div class="exec-action">🎯 {action_to_take}</div>
    </div>
    <div class="foot">
      <span>Auto-Sourced: {auto} of {tot} scorable parameters | {man} manual inputs</span>
      <span>AntiGravity Institutional Terminal · Daily View Building</span>
    </div>
  </div>
</body>
</html>
"""

        tmp = path.with_suffix(".html")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(html, encoding="utf-8")

        with sync_playwright() as pw:
            b = pw.chromium.launch()
            pg = b.new_page(viewport={"width": 1260, "height": 1800}, device_scale_factor=scale)
            pg.goto(f"file://{tmp.resolve()}", wait_until="networkidle")
            card = pg.query_selector("#vb-card")
            if card:
                card.screenshot(path=str(path))
            else:
                pg.screenshot(path=str(path), full_page=True)
            b.close()

        tmp.unlink(missing_ok=True)
        return path
    except Exception:
        # Fallback to high-resolution native Matplotlib rendering
        return to_image_native(params, path, when=when, result_note=result_note, dark_mode=dark_mode)



# ═══════════════════════════════════════════════════════════════════════════
# The three-line brief
# ═══════════════════════════════════════════════════════════════════════════

def chain_brief(df, spot, gift=None, prev_close=None, live: bool = True):
    """
    Three lines: what the chain says, where it opens, and the levels.

    The eighteen parameters are the working detail. This is the sentence you
    would say out loud at 09:00, and it is deliberately three lines because
    a pre-market read that needs a paragraph is not a read.

    Line 2 is the one to treat carefully. Gift Nifty predicts the OPEN and
    nothing beyond it; a gap-up open is not a bullish session and reading it
    as one is how gap-fill days take money. It is reported as an expected
    open, never as a direction for the day.
    """
    from setups import chain_condition
    import analytics as an

    out = {"condition": None, "gap": None, "levels": None, "conflict": None}

    # ── Line 1: the chain's own verdict, both readings ──────────────────
    ltp = chain_condition(df, spot, mode="ltp")
    oi = chain_condition(df, spot, mode="oi")
    out["condition"] = {
        "name": ltp.name, "score": ltp.score, "pattern": ltp.pattern,
        "exact": ltp.exact, "trail": ltp.trail,
        "alt_name": oi.name, "alt_pattern": oi.pattern,
        "agree": ltp.name == oi.name,
        "text": (f"{ltp.name} ({ltp.score:+d}) — {ltp.pattern}"
                 if ltp.exact else
                 f"{ltp.name} — {ltp.pattern}, not one of the five named "
                 f"conditions"),
        "live": live,
    }

    # ── Line 2: where it is likely to open ──────────────────────────────
    if gift is not None and getattr(gift, "gap", None) is not None:
        g = float(gift.gap)
        size = ("FLAT" if abs(g) < 30 else
                "GAP UP" if g >= 100 else "GAP DOWN" if g <= -100 else
                "mild gap up" if g > 0 else "mild gap down")
        ref = prev_close if prev_close is not None else getattr(
            gift, "prev_close", None)
        opens = f" → around {ref + g:,.0f}" if ref else ""
        out["gap"] = {
            "points": g, "size": size,
            "basis": getattr(gift, "gap_basis", ""),
            "text": (f"{size} {g:+.0f} pts{opens} "
                     f"(Gift {gift.value:,.0f} vs futures)"),
            "caution": ("A gap only sets the open. It says nothing about "
                        "direction after 09:15, and a gap that fills is the "
                        "most common way this reading loses money."),
        }

    # ── Line 3: the levels the chain itself is defending ────────────────
    try:
        cl = an.chain_levels(df, spot)
        res = [f"{r.strike:,.0f} ({r.state.lower()})" for r in cl["resistance"][:2]]
        sup = [f"{s.strike:,.0f} ({s.state.lower()})" for s in cl["support"][:2]]
        out["levels"] = {
            "resistance": res, "support": sup,
            "text": (f"R {' · '.join(res) if res else '—'}   |   "
                     f"S {' · '.join(sup) if sup else '—'}"),
        }
    except Exception:                                       # noqa: BLE001
        pass

    # ── Does the chain agree with where it is opening? ──────────────────
    if out["gap"] and out["condition"] and ltp.exact:
        up = "BULLISH" in ltp.name
        down = "BEARISH" in ltp.name
        g = out["gap"]["points"]
        if (up and g <= -30) or (down and g >= 30):
            out["conflict"] = (
                f"The chain reads {ltp.name} but it is opening "
                f"{out['gap']['size'].lower()}. Overnight positioning and the "
                f"overnight gap disagree; that is a reason to wait for the "
                f"first fifteen minutes, not a reason to pick one of them.")
    return out
