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
    #15 Option chain condition    setups.chain_condition, at 9:15

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

import numpy as np
import pandas as pd

import config

SECTOR_KEYS = ("NIFTY IT", "NIFTY BANK", "NIFTY REALTY", "NIFTY AUTO",
               "NIFTY METAL", "NIFTY FMCG", "NIFTY PHARMA", "NIFTY ENERGY")


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
        The status word or phrase in the View column.
        Preserves custom multi-word states (e.g. BULLISH CONSOLIDATION, BEARISH CONSOLIDATION,
        CONSOLIDATION, BULLISH, BEARISH, or "") when explicitly set, otherwise defaults to the
        sign of the score.
        """
        if self.view is not None and self.view != "":
            return self.view
        if self.score is None:
            return "NA"
        if self.score > 0:
            return "BULLISH"
        if self.score < 0:
            return "BEARISH"
        return "NEUTRAL"


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
        p.score, p.view = 2, "BULLISH — fear LOW, advantage to buyers"
    elif chg > 4:
        p.score, p.view = -2, "BEARISH — high fear, setups may fail, reduce size"
    else:
        p.score, p.view = 0, "NEUTRAL — calm, both setups work, full size"
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


def p6_daily_trend(daily: pd.DataFrame) -> Param:
    """HH/HL above the 8 EMA with UBB rising = +2, the mirror = −2."""
    p = Param(6, "Nifty 50 Trend & Behaviour on Daily Time Frame", "2")
    if daily is None or len(daily) < 25:
        p.note = "needs 25+ daily candles in data/ohlc"
        return p

    d = daily.tail(120).copy()
    d["ema8"] = d["Close"].ewm(span=8, adjust=False).mean()
    mid = d["Close"].rolling(20).mean()
    sd = d["Close"].rolling(20).std(ddof=0)
    d["ubb"], d["lbb"] = mid + 2 * sd, mid - 2 * sd

    last = d.iloc[-1]
    above = last["Close"] > last["ema8"]

    bb = band_behaviour(d)
    ubb_up = bb.get("ubb_dir") == "UP"
    lbb_dn = bb.get("lbb_dir") == "DOWN"

    # HH/HL over the last 20 sessions
    recent = d.tail(20)
    hh = recent["High"].iloc[-1] >= recent["High"].max() * 0.995
    ll = recent["Low"].iloc[-1] <= recent["Low"].min() * 1.005

    p.observation = (f"NIFTY {'ABOVE' if above else 'BELOW'} 8 EMA "
                     f"({last['ema8']:,.0f}); "
                     f"UBB {bb.get('ubb_dir','?')} & LBB {bb.get('lbb_dir','?')}; "
                     f"bands {bb.get('shape','?')}")
    p.auto = True
    if bb:
        p.note = (f"{bb['read']} · width {bb['width']:,.0f} pts "
                  f"({bb['width_change_pct']:+.1f}% over 3 sessions)")

    # 50 EMA across the three timeframes — the structural filter
    ep = ema_position(daily, 50)
    if ep:
        parts = []
        for tf in ("1D", "1W", "1M"):
            v = ep.get(tf)
            if not v or v["above"] is None:
                continue
            parts.append(f"{tf} {'ABOVE' if v['above'] else 'BELOW'} 50EMA "
                         f"({v['ema']:,.0f})")
        if parts:
            p.observation += " | " + " · ".join(parts)
            n_above = sum(1 for tf in ("1D", "1W", "1M")
                          if ep.get(tf, {}).get("above"))
            p.note += (f" · {n_above}/3 timeframes above the 50 EMA")
    if above and ubb_up and hh:
        p.score, p.view = 2, "BULLISH — HH/HL uptrend above 8 EMA"
    elif (not above) and lbb_dn and ll:
        p.score, p.view = -2, "BEARISH — LH/LL downtrend below 8 EMA"
    elif above and ubb_up:
        p.score, p.view = 2, "BULLISH — above 8 EMA, UBB rising"
    elif (not above) and lbb_dn:
        p.score, p.view = -2, "BEARISH — below 8 EMA, LBB falling"
    else:
        p.score, p.view = 0, "NEUTRAL — consolidation, bands flat"
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

    d = daily.set_index("Date")
    wk = d.resample("W").agg({"Open": "first", "High": "max",
                              "Low": "min", "Close": "last"}).dropna()
    mo = d.resample("ME").agg({"Open": "first", "High": "max",
                               "Low": "min", "Close": "last"}).dropna()

    rd, rw, rm = _rsi(d["Close"]), _rsi(wk["Close"]), _rsi(mo["Close"])
    kd, _ = _stoch_rsi(d["Close"])
    kw, _ = _stoch_rsi(wk["Close"])
    km, _ = _stoch_rsi(mo["Close"])

    p.observation = (f"RSI STOCH @ {kd:.0f} LEVELS (DAILY); "
                     f"{kw:.0f} LEVELS (WEEKLY); {km:.0f} (MONTHLY)")
    p.auto = True

    run, state = _extreme_run(d["Close"])

    # Scored on DAILY, as your notes specify for intraday. WHICH oscillator
    # scores is set by config.RSI_SCORE_MODE — see the note there.
    mode = getattr(config, "RSI_SCORE_MODE", "rsi")
    if mode == "stochrsi":
        hot, cold = kd > 80, kd < 20
    elif mode == "either":
        hot, cold = (rd > 80 or kd > 80), (rd < 20 or kd < 20)
    else:
        hot, cold = rd > 80, rd < 20

    if hot:
        p.score, p.view = -2, "BEARISH — overbought, A-shape risk, do NOT trail"
    elif cold:
        p.score, p.view = 2, "BULLISH — oversold, reversal potential"
    else:
        p.score, p.view = 0, "NEUTRAL — 20-80 zone"

    notes = []

    # PERSISTENCE. A reading that has been pinned at the extreme for days is a
    # different animal from one that arrived this morning: the longer it
    # stays, the more stretched the move and the closer the reversal your
    # notes describe as A-shape (or its mirror on the oversold side).
    ks = _stoch_rsi_series(d["Close"])
    hot_days = _streak(ks, above=80)
    cold_days = _streak(ks, below=20)
    if hot_days >= 2:
        notes.append(f"Stoch RSI overbought {hot_days} sessions running — "
                     f"stretched, A-shape risk building, do NOT trail")
    elif cold_days >= 2:
        notes.append(f"Stoch RSI oversold {cold_days} sessions running — "
                     f"stretched to the downside, reversal building")

    # One line each on the higher timeframes, for context on the sheet.
    def band(v):
        return "overbought" if v > 80 else ("oversold" if v < 20 else "mid-zone")
    notes.append(f"weekly {band(kw)} ({kw:.0f}), monthly {band(km)} ({km:.0f})")
    if kw > 80 and kd < 20:
        notes.append("daily oversold inside an overbought weekly — bounce, "
                     "not a trend change")
    elif kw < 20 and kd > 80:
        notes.append("daily overbought inside an oversold weekly — relief "
                     "rally, not a trend change")

    rsi_x = "hot" if rd > 80 else ("cold" if rd < 20 else "mid")
    st_x = "hot" if kd > 80 else ("cold" if kd < 20 else "mid")
    if rsi_x != st_x:
        notes.append(f"RSI is {rsi_x} while Stoch RSI is {st_x}")
    if rw > 80:
        notes.append(f"weekly RSI {rw:.0f} overbought — higher-timeframe A-shape risk")
    elif rw < 20:
        notes.append(f"weekly RSI {rw:.0f} — major investing signal per Sushen's session")
    p.note = " · ".join(notes)
    return p


def p8_p9_levels(master=None) -> tuple[Param, Param]:
    """
    Supports and resistances for the sheet.

    Shows only what your course teaches — prior swing levels and unfilled gap
    zones. Gann levels and time cycles are computed elsewhere in the app and
    are visible on the Master levels tab, but they are deliberately kept OFF
    this sheet: MJ does not teach them, and putting them on homework he marks
    would invite questions you cannot answer from the course material.
    """
    s = Param(8, "3 important supports for the day", "NA")
    r = Param(9, "3 important Resistance for the day", "NA")
    if master is None or master.empty:
        s.note = r.note = "run build_master.py first"
        return s, r
    row = master.iloc[-1]

    def fmt(cols, labels, gaps):
        lv = [f"{lab}@{row[c]:,.0f}"
              for c, lab in zip(cols, labels) if pd.notna(row.get(c))]
        gp = [f"{row[g]:,.0f}" for g in gaps if pd.notna(row.get(g))]
        out = ", ".join(lv)
        if gp:
            out += f" | {len(gp)} gap zone(s): {', '.join(gp)}"
        return out

    s.observation = fmt(["S1", "S2", "S3"], ["S1", "S2", "S3"],
                        ["Lower_Gap_1", "Lower_Gap_2", "Lower_Gap_3"])
    r.observation = fmt(["R1", "R2", "R3"], ["R1", "R2", "R3"],
                        ["Upper_Gap_1", "Upper_Gap_2", "Upper_Gap_3"])
    s.view = "Avoid SHORT extensions near S1/S2"
    r.view = "Avoid LONG extensions near R1/R2"
    s.note = r.note = "prior swing levels + unfilled gaps (no Gann, no cycles)"
    s.auto = r.auto = True
    return s, r


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
            a.score, a.view = 2, f"BULLISH — {adv}/50 advancing, strong breadth"
        elif adv >= 30:
            a.score, a.view = 1, f"MILDLY BULLISH — {adv}/50 advancing"
        elif adv < 12:
            a.score, a.view = -2, f"BEARISH — only {adv}/50 advancing"
        elif adv <= 20:
            a.score, a.view = -1, f"MILDLY BEARISH — only {adv}/50 advancing"
        else:
            a.score, a.view = 0, f"NEUTRAL — {adv}/50 advancing"
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
            b.score, b.view = 2, f"BULLISH — {adv} of {total} banks advancing"
        elif total and dec / total >= 0.70:
            b.score, b.view = -2, f"BEARISH — {dec} of {total} banks declining"
        else:
            b.score, b.view = 0, f"NEUTRAL — mixed ({adv} up, {dec} down)"
        if bn_ad.get("timestamp"):
            b.note = f"as on {bn_ad['timestamp']}"
    else:
        b.note = "advance/decline endpoint unavailable"
    return a, b


def p12_sectors(indices: list[dict]) -> Param:
    p = Param(12, "Sectoral Indices", "2")
    found = []
    for r in indices:
        nm = str(r.get("index", r.get("indexSymbol", ""))).upper()
        if nm in SECTOR_KEYS:
            chg = float(r.get("percentChange") or r.get("pChange") or 0)
            tag = "P" if chg > 0.15 else ("N" if chg < -0.15 else "F")
            found.append((nm.replace("NIFTY ", "").title(), tag, chg))
    if not found:
        p.note = "no sector indices matched"
        return p

    p.observation = " · ".join(f"{n} ({t} {c:+.2f}%)" for n, t, c in found)
    p.auto = True
    pos = sum(1 for _, t, _ in found if t == "P")
    neg = sum(1 for _, t, _ in found if t == "N")
    # per your notes: FLAT counts as neutral, it must not drag the score down
    if pos >= 3 and pos > neg:
        p.score, p.view = 2, f"BULLISH — {pos} sectors positive"
    elif neg >= 3 and neg > pos:
        p.score, p.view = -2, f"BEARISH — {neg} sectors negative"
    else:
        p.score, p.view = 0, "NEUTRAL — mixed / flat"
    return p


def p13_pcr(pcr: float | None) -> Param:
    p = Param(13, "PCR ( Put-Call Ratio)", "NA")
    if pcr is None or not np.isfinite(pcr):
        p.note = "option chain unavailable"
        return p
    p.observation = f"Nifty PCR @ {pcr:.2f}"
    p.auto = True
    if pcr > 1.4:
        p.view = "OVERBOUGHT context — heavy puts, market may fall"
    elif pcr < 0.6:
        p.view = "OVERSOLD context — heavy calls, market may bounce"
    else:
        p.view = "NEUTRAL — 0.6 to 1.4 range"
    return p


def p14_fii(flows, poi=None) -> Param:
    """
    Scored from FII INDEX FUTURES, per your notes — cash is context only.

    Source is NSE's own participant-wise open interest file, a static CSV on
    nsearchives. That is where the aggregator sites get it, so this reads the
    original rather than someone's re-publication of it, and it will keep
    working when NSE next reorganises their web API.

    Verified against a live file: FII Future Index Long 24,870 minus Short
    234,677 gives net -209,807, matching the third-party figure exactly.
    """
    p = Param(14, "FII & DII Data", "2")
    bits = []
    if flows and flows.fii_cash_net is not None:
        bits.append(f"FII CM {flows.fii_cash_net:+,.0f}Cr")
        bits.append(f"DII CM {flows.dii_cash_net or 0:+,.0f}Cr")

    if poi is not None:
        bits.append(f"FII IDX FUT net {poi.fii_net:+,} contracts")
        if poi.fii_change is not None:
            bits.append(f"change {poi.fii_change:+,}")
        p.observation = " | ".join(bits)
        p.score, p.view = poi.score()
        p.auto = True
        p.note = (f"As on {poi.date}. Long {poi.fii_idx_fut_long:,} vs short "
                  f"{poi.fii_idx_fut_short:,}. DII net {poi.dii_net:+,}, "
                  f"Client net {poi.client_net:+,}. Scored on the DAY'S "
                  f"CHANGE (the flow), not the standing position.")
    else:
        p.observation = " | ".join(bits + ["FII IDX Fut ___ (unavailable)"])
        p.auto = bool(bits)
        p.note = ("Participant-wise OI file not reachable — NSE publishes it "
                  "in the evening. Cash figures alone must NOT be scored.")
    return p


def p15_narrative(cond=None, candles: pd.DataFrame | None = None, spot: float | None = None,
                  session_stats: dict | None = None,
                  custom_narrative: str = "", custom_action: str = "",
                  custom_state: str = "", live: bool = True, stamp: str = "") -> Param:
    """
    #15 — Market Narrative & Action (9:15–3:45 PM).
    Synthesizes session price action, intraday range structure, and option chain flow:
      • Narrative of what happened from 9:15 to 3:45 PM
      • Concrete action to take
      • Market State: BULLISH, BEARISH, BULLISH CONSOLIDATION, BEARISH CONSOLIDATION, CONSOLIDATION, or ""
    """
    p = Param(15, "Market Narrative & Action (9:15–3:45 PM)", "2")

    # 1. Custom / Manual input takes precedence
    if custom_narrative or custom_state:
        state = custom_state.strip().upper() if custom_state else ""
        if state == "BULLISH":
            score = 2
            default_action = "Action: Buy dips near VWAP/support with trailing SL · Target: 100–400 pts"
        elif state == "BEARISH":
            score = -2
            default_action = "Action: Fade rallies near resistance with trailing SL · Target: 100–400 pts"
        elif state in ("BULLISH CONSOLIDATION", "BULLISH CONSOL"):
            score = 1
            state = "BULLISH CONSOLIDATION"
            default_action = "Action: Buy near support / lower band · Exit strictly at 40 pts only"
        elif state in ("BEARISH CONSOLIDATION", "BEARISH CONSOL"):
            score = -1
            state = "BEARISH CONSOLIDATION"
            default_action = "Action: Fade near resistance / upper band · Exit strictly at 40 pts only"
        elif state in ("CONSOLIDATION", "NEUTRAL"):
            score = 0
            state = "CONSOLIDATION"
            default_action = "Action: Range-bound session · Quick scalps (exit at 40 pts) or stay on sidelines"
        else:
            score = 0
            state = custom_state
            default_action = "Action: Monitor price action and range boundaries"

        action = custom_action.strip() or default_action
        narrative = custom_narrative.strip() or f"Session narrative: {state} structure from 9:15 to 3:45 PM."
        p.observation = f"{narrative} — {action}" if action else narrative
        p.score = score
        p.view = state
        p.auto = False
        p.note = f"State: {state} | {action}"
        return p

    # 2. Auto-generate from candles and option chain flow
    if candles is None or (isinstance(candles, pd.DataFrame) and candles.empty):
        try:
            import price_action as pa
            candles = pa.load_stored()
            if candles is None or candles.empty:
                c_files = sorted(config.DATA_DIR.glob("candles/202*.csv"))
                if c_files:
                    candles = pd.read_csv(c_files[-1])
                    if "timestamp" in candles.columns:
                        candles["timestamp"] = pd.to_datetime(candles["timestamp"])
        except Exception:
            pass

    if cond is None:
        try:
            import chain_parser as cp
            import setups as su
            chain_path = config.DATA_DIR / "sample_chain.csv"
            if chain_path.exists():
                c_df = cp.parse_csv(str(chain_path))
                c_spot = cp.infer_spot(c_df)
                cond = su.chain_condition(c_df, c_spot)
        except Exception:
            pass

    ci = getattr(cond, "call_itm", "R") if cond else "R"
    co = getattr(cond, "call_otm", "R") if cond else "R"
    pi = getattr(cond, "put_itm", "R") if cond else "R"
    po = getattr(cond, "put_otm", "R") if cond else "R"
    quad_key = (ci, co, pi, po)

    has_candles = (
        candles is not None
        and isinstance(candles, pd.DataFrame)
        and not candles.empty
        and len(candles) >= 3
    )
    if has_candles:
        open_p = float(candles["open"].iloc[0])
        high_p = float(candles["high"].max())
        low_p = float(candles["low"].min())
        close_p = float(candles["close"].iloc[-1])
        range_pts = high_p - low_p
        chg_open = close_p - open_p
        pos_in_range = (close_p - low_p) / range_pts if range_pts > 0 else 0.5
    else:
        open_p = spot or 24000.0
        high_p = open_p
        low_p = open_p
        close_p = open_p
        range_pts = 0.0
        chg_open = 0.0
        pos_in_range = 0.5

    if quad_key == ("G", "G", "R", "R") or (chg_open > 35 and pos_in_range > 0.6 and pi == "R"):
        state = "BULLISH"
        score = 2
        action = "Action: Buy dips near VWAP/support with trailing SL · Target: 100–400 pts"
        flow_desc = "Call buying dominant (ITM/OTM Green)" if (ci == "G" and co == "G") else "strong upward momentum holding above VWAP"
        narrative = f"9:15–15:45: NIFTY opened at {open_p:,.0f}, expanded range to high {high_p:,.0f} ({chg_open:+.0f} pts from open); {flow_desc}"
    elif quad_key == ("R", "R", "G", "G") or (chg_open < -35 and pos_in_range < 0.4 and ci == "R"):
        state = "BEARISH"
        score = -2
        action = "Action: Fade rallies near resistance with trailing SL · Target: 100–400 pts"
        flow_desc = "Put buying dominant (ITM/OTM Green)" if (pi == "G" and po == "G") else "persistent selling pressure trading below VWAP"
        narrative = f"9:15–15:45: NIFTY opened at {open_p:,.0f}, slipped to day's low {low_p:,.0f} ({chg_open:+.0f} pts from open); {flow_desc}"
    elif quad_key == ("G", "R", "R", "R") or (ci == "G" and (chg_open >= 0 or range_pts < 85)):
        state = "BULLISH CONSOLIDATION"
        score = 1
        action = "Action: Buy near support / lower band · Exit strictly at 40 pts only"
        flow_desc = "Call ITM buying with Call/Put selling capping extension"
        narrative = f"9:15–15:45: NIFTY consolidating in {range_pts:.0f} pt band ({low_p:,.0f}–{high_p:,.0f}) holding above open; {flow_desc}"
    elif quad_key == ("R", "R", "G", "R") or (pi == "G" and (chg_open < 0 or range_pts < 85)):
        state = "BEARISH CONSOLIDATION"
        score = -1
        action = "Action: Fade near resistance / upper band · Exit strictly at 40 pts only"
        flow_desc = "Put ITM buying with Call selling capping upside"
        narrative = f"9:15–15:45: NIFTY consolidating in {range_pts:.0f} pt band ({low_p:,.0f}–{high_p:,.0f}) holding below open; {flow_desc}"
    else:
        state = "CONSOLIDATION"
        score = 0
        action = "Action: Range-bound session · Quick scalps (exit at 40 pts) or stay on sidelines"
        if has_candles and range_pts > 0:
            narrative = f"9:15–15:45: NIFTY rangebound in {range_pts:.0f} pt band ({low_p:,.0f}–{high_p:,.0f}); two-way option writing with no directional breakout"
        else:
            narrative = "9:15–15:45: Session tracking live; awaiting range expansion beyond initial opening band"

    p.observation = f"{narrative} — {action}"
    p.auto = True
    p.view = state
    p.score = score
    p.note = f"State: {state} | {action}"
    return p


p15_chain = p15_narrative  # Backward compatibility alias



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
        p.score, p.view = -1, "BEARISH — above Rs6,500"
    else:
        p.score, p.view = 1, "BULLISH — below Rs6,500"
    p.note = (f"FALLBACK: WTI ${crude.wti_usd:,.2f} x USDINR "
              f"{crude.usdinr:.2f}. NSE's own crude contract was unreachable, "
              f"so this is a proxy and can differ by Rs100+.")
    return p


def p16_global(glob=None) -> Param:
    p = Param(16, "Global Markets", "2")
    if glob is None or not glob.rows:
        p.observation = ("NIKKEI ___ HANGSENG ___ KOSPI ___ US ___ Europe ___")
        p.note = "investing.com unreachable"
        return p
    p.observation = glob.summary
    p.score, p.view = glob.score()
    p.auto = True
    p.note = ("Scored on FRESH rows only. investing.com serves cached data to "
              "scripts — closed markets can come back weeks stale, so any row "
              "without a clock timestamp is shown but not scored.")
    return p


def p5_gift(gift=None) -> Param:
    """NA — observation only. It predicts the gap, it does not score the day."""
    p = Param(5, "Gap Up or Gap Down", "NA")
    if gift is None:
        p.observation = "Gift Nifty @____ (-) Nifty Fut @____ = ___ pts"
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
    p.view = gift.verdict
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
            p.observation = ("Nifty @____ (-) Nifty Fut @____ = ___ pts"
                             if no == 1 else
                             "B Nifty @____ (-) BNF Fut @____ = ___ pts")
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
        p.score, p.view = 1, "BULLISH — futures at premium"
    elif gap < -5:
        p.score, p.view = -1, "BEARISH — futures at discount"
    else:
        p.score, p.view = 0, "NEUTRAL — futures near spot"
    p.note = ("Futures quote is investing.com's CFD, an indicative price — "
              "verify against Kite if the basis looks odd.")
    return p


def p17_adrs(adrs=None) -> Param:
    p = Param(17, "US ADR", "2")
    if adrs is None or not adrs.rows:
        p.observation = "Infosys ___ Wipro ___ ICICI ___ HDFC ___ Dr Reddy ___"
        p.note = "investing.com unreachable — fill by hand"
        return p
    p.observation = adrs.observation
    p.score, p.view = adrs.score()
    p.auto = True
    stale = [r["name"] for r in adrs.rows if r["stale"]]
    p.note = "B = rose in the previous US session, S = fell."
    if stale:
        p.note += f" Starred rows are STALE and excluded from the score: {', '.join(stale)}."
    return p


def p18_usdinr(gift=None) -> Param:
    """
    USD/INR — an addition of yours, beyond MJ's 17.

    Deliberately NA (unscored) so your total still matches MJ's scale and
    stays comparable with the sheet he marks. A weakening rupee pressures FII
    flows, so it is worth watching — but ask him whether he wants it scored
    before changing the arithmetic on a sheet you submit.

    Sourced from NSE's own USDINR futures via the getGiftNifty payload, not a
    third-party spot rate.
    """
    p = Param(18, "USD / INR", "NA")
    fx = getattr(gift, "usd_inr", None) if gift else None
    if fx is None:
        p.observation = "USD/INR @ ____"
        p.note = "not available"
        return p
    p.observation = f"USD/INR futures @ {fx:.3f}"
    p.auto = True
    if fx >= 96:
        p.view = "Rupee weak — pressures FII inflows"
    elif fx <= 92:
        p.view = "Rupee strong — supportive of FII inflows"
    else:
        p.view = "Rupee in its normal band"
    upd = getattr(gift, "usd_inr_updated", "") if gift else ""
    p.note = ("NSE USDINR futures" + (f", updated {upd}" if upd else "")
              + ". Unscored so your total still matches MJ's 17-parameter scale.")
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
          custom_narrative: str = "",
          custom_action: str = "",
          custom_state: str = "",
          overrides: dict[int, Any] | None = None) -> list[Param]:
    indices = indices or []
    overrides = overrides or {}

    f1, f2 = p1_p2_futures(nifty_fut, bn_fut)
    s8, r9 = p8_p9_levels(master)
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
        p13_pcr(pcr),
        p14_fii(flows, poi),
        p15_narrative(chain_cond, candles=candles, spot=spot,
                      custom_narrative=custom_narrative,
                      custom_action=custom_action,
                      custom_state=custom_state),
        p16_global(glob),
        p17_adrs(adrs),
        p18_usdinr(gift),
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
                    p.view = vw
            elif isinstance(ov, str):
                p.observation = ov
    return params


def total_score(params: list[Param]) -> int:
    return sum(p.score for p in params if p.scored)


def verdict(score: int) -> tuple[str, str]:
    if score > 16:
        return "BULLISH", "green"
    if score < 7:
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

def to_excel(params: list[Param], path: Path, when: date | None = None) -> Path:
    """
    Write the sheet in your existing format so the screenshot you send MJ
    looks exactly like the one you have been filling by hand.
    """
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
                   "NA" if not p.scored else p.score, p.view or p.note])
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

    ws.append([])
    ws.append(["TOTAL", "", "", "SCORE > 16 = BULLISH | 7-16 = NEUTRAL | < 7 = BEARISH",
               score, vname])
    tr = ws.max_row
    for i in range(1, 7):
        ws.cell(row=tr, column=i).font = Font(name=ARIAL, bold=True)
        ws.cell(row=tr, column=i).fill = {"green": green, "red": red,
                                          "amber": amber}[vcol]

    auto, tot, man = coverage(params)
    ws.append([])
    ws.append(["", "STRATEGY RULE: Follow session narrative and structural levels. Adapt bias on VWAP breach."])
    ws.append(["", f"Green cells = auto-filled from NSE ({auto} of {tot} "
                   f"scorable). Yellow cells = fill by hand ({man} left)."])
    for rr in (ws.max_row - 1, ws.max_row):
        ws.cell(row=rr, column=2).font = Font(name=ARIAL, italic=True, size=9)

    widths = {1: 7, 2: 38, 3: 11, 4: 58, 5: 8, 6: 34}
    for i, w in widths.items():
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = f"A{hrow + 1}"

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


# ═══════════════════════════════════════════════════════════════════════════
# PNG export — for WhatsApp (Publication Grade HD Image Card)
# ═══════════════════════════════════════════════════════════════════════════

def _extract_narrative_and_action(params: list[Param], default_verdict: str) -> tuple[str, str, str]:
    """Extracts session narrative, action to take, and market state from Parameter 15."""
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
        narrative = obs or "Intraday session tracked across 9:15 to 3:45 PM."
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
    row_h = 0.0395
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
        wrapped = textwrap.wrap(obs_raw, width=62)
        if len(wrapped) > 2:
            obs_display = wrapped[0] + "\n" + wrapped[1][:58] + "…"
        elif len(wrapped) == 2:
            obs_display = wrapped[0] + "\n" + wrapped[1]
        else:
            obs_display = obs_raw
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
        if dark_mode:
            vw_fg = "#00E676" if "BULLISH" in vw else "#FF5252" if "BEARISH" in vw else "#38BDF8" if "CONSOL" in vw or "NEUTRAL" in vw else "#94A3B8"
            vw_bg = "#0D3322" if "BULLISH" in vw else "#3B1414" if "BEARISH" in vw else "#132E4A" if "CONSOL" in vw or "NEUTRAL" in vw else "#1E293B"
        else:
            vw_fg = "#008744" if "BULLISH" in vw else "#D32F2F" if "BEARISH" in vw else "#1F5FA8" if "CONSOL" in vw or "NEUTRAL" in vw else "#64748B"
            vw_bg = "#E7F6EC" if "BULLISH" in vw else "#FDECEA" if "BEARISH" in vw else "#EAF1FA" if "CONSOL" in vw or "NEUTRAL" in vw else "#F1F5F9"

        ax.add_patch(patches.FancyBboxPatch((0.835, y_curr + 0.005), 0.13, 0.028, boxstyle="round,pad=0.004", facecolor=vw_bg, edgecolor="none", transform=ax.transAxes))
        ax.text(0.90, y_curr + 0.013, vw_short, color=vw_fg, fontsize=10.5, weight="bold", ha="center", transform=ax.transAxes)

    # 4. EXECUTIVE SUMMARY & SESSION NARRATIVE Box
    y_curr -= 0.030
    ax.add_patch(patches.Rectangle((0.02, y_curr), 0.96, 0.028, facecolor=table_hdr_bg))
    ax.text(0.50, y_curr + 0.008, "EXECUTIVE SUMMARY & SESSION NARRATIVE (9:15 AM – 3:45 PM)",
            color="#FFD400", fontsize=12, weight="bold", ha="center", transform=ax.transAxes)

    # Narrative Content Box
    narr_box_h = 0.095
    y_curr -= narr_box_h
    box_bg = "#111E33" if dark_mode else "#FFFDEB"
    box_edge = "#38BDF8" if dark_mode else "#E2B93B"
    ax.add_patch(patches.Rectangle((0.02, y_curr), 0.96, narr_box_h, facecolor=box_bg, edgecolor=box_edge, linewidth=1.5))

    # Top line: Score & State
    ax.text(0.04, y_curr + 0.070, f"TOTAL SCORE: {score:+d} / 34 · {vname} BIAS",
            color=fg_text, fontsize=13.5, weight="bold", transform=ax.transAxes)
    ax.text(0.96, y_curr + 0.070, f"MARKET STATE: {sess_state}",
            color="#FF5252" if "BEARISH" in sess_state else "#00E676" if "BULLISH" in sess_state else "#38BDF8" if dark_mode else "#1F5FA8",
            fontsize=13, weight="bold", ha="right", transform=ax.transAxes)

    # Middle line: Session narrative
    narr_wrap = textwrap.wrap(narrative, width=105)
    narr_text = "\n".join(narr_wrap[:2])
    ax.text(0.04, y_curr + 0.043, narr_text, color=fg_text, fontsize=11, transform=ax.transAxes)

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
    session narrative (9:15 to 3:45 PM), concrete action plan, and market state badge.
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
                "BULLISH CONSOLIDATION": ("#26E7A6", "#0E382B"),
                "BULLISH CONSOL": ("#26E7A6", "#0E382B"),
                "BEARISH CONSOLIDATION": ("#FF7A45", "#401C14"),
                "BEARISH CONSOL": ("#FF7A45", "#401C14"),
                "CONSOLIDATION": ("#38BDF8", "#132E4A"),
                "NEUTRAL": ("#38BDF8", "#132E4A"),
                "NA": ("#94A3B8", "#1E293B"),
                "": ("#94A3B8", "#1E293B"),
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
                "BULLISH CONSOLIDATION": ("#0D8A50", "#E6F7EF"),
                "BULLISH CONSOL": ("#0D8A50", "#E6F7EF"),
                "BEARISH CONSOLIDATION": ("#D9480F", "#FFF0F6"),
                "BEARISH CONSOL": ("#D9480F", "#FFF0F6"),
                "CONSOLIDATION": ("#1F5FA8", "#EAF1FA"),
                "NEUTRAL": ("#1F5FA8", "#EAF1FA"),
                "NA": ("#64748B", "#F1F5F9"),
                "": ("#64748B", "#F1F5F9"),
            }

        state_fg, state_bg = word_styles.get(sess_state, word_styles.get("NEUTRAL"))

        rows_html = []
        for p_ in params:
            w = p_.verdict_word
            fg, bg = word_styles.get(w, ("#64748B", "#F1F5F9"))
            sc = "NA" if not p_.scored else f"{p_.score:+d}"
            sc_color = sc_pos if p_.scored and p_.score > 0 else sc_neg if p_.scored and p_.score < 0 else sc_zero
            obs = (p_.observation or "—").replace("<", "&lt;").replace(">", "&gt;")
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
    <div class="exec-hdr">EXECUTIVE SUMMARY & SESSION NARRATIVE (9:15 AM – 3:45 PM)</div>
    <div class="exec-box">
      <div class="exec-top">
        <div class="exec-score">TOTAL SCORE: {score:+d} / 34 · {vname} BIAS</div>
        <div class="exec-state">MARKET STATE: <span class="pill" style="color:{state_fg};background:{state_bg};border:1px solid {state_fg}40;">{sess_state}</span></div>
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
