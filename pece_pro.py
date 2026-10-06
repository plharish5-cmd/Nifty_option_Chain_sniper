"""
pece_pro.py — the "★ PRO READ" column for the PE − CE OI Table (pe_ce_panel.py).

The Signal column reads ONE candle in isolation. Experienced OI readers never do that; they ask five more questions,
and this column answers them for every row, using only data up to that row (no look-ahead):

  1. WHEN   — after 15:30 (closing-price print, frozen spot) and expiry-afternoon unwinding are NOT signals.
  2. HOW BIG — this candle's PE − CE change vs the day's normal candle (●●● ≥ 3×, ●● ≥ 1.5×, ● normal, · noise).
               A "Put unwinding" of 2 % of normal is noise; most single-candle labels are.
  3. TREND  — does the PE − CE (Day) line over the last ~15 min agree with price?  Writers + price together = trend
               with backing; a candle against that trend = usually a pause, not a reversal.
  4. DIVERGENCE — price makes a new 30-min high but PE − CE (Day) does not (writers not following) → rally running on
               empty; mirror at lows.  The single most useful PE − CE read.
  5. WALLS  — the strike with the most put OI (floor) and call OI (ceiling) inside the range: when the floor moves UP or
               the ceiling moves DOWN, writers are re-positioning — shown as a second line in the cell.
  + ABSORPTION — a big writer candle with almost no price move = writers absorbing aggressive buyers / sellers.

None of these is backtested yet — run backtest_pece_pro.py on your stored days to see which tags carry an edge.
Information only — not financial advice.
"""
from __future__ import annotations

import html
from datetime import time

import numpy as np
import pandas as pd

CLOSE = time(15, 30)
UNWIND_FROM = time(13, 30)


# ─── per snapshot (called from pe_ce_panel.per_snapshot) ──────────────────────
def extra(d: pd.DataFrame, ts) -> dict:
    """Floor / ceiling strikes in the window + expiry-day flag. A wall counts only if it beats the 2nd strike by 5 %."""
    out = {"pe_wall": np.nan, "ce_wall": np.nan, "exp_day": False}
    try:
        k = pd.to_numeric(d["strike"], errors="coerce")
        for side in ("pe", "ce"):
            oi = pd.to_numeric(d[f"{side}_oi"], errors="coerce").fillna(0).values
            if len(oi) >= 2:
                o = np.argsort(oi)[::-1]
                if oi[o[0]] > 0 and oi[o[0]] >= 1.05 * oi[o[1]]:
                    out[f"{side}_wall"] = float(k.iloc[o[0]])
        if "expiry" in d and len(d):
            e = pd.to_datetime(d["expiry"].iloc[0], errors="coerce")
            out["exp_day"] = bool(pd.notna(e) and e.date() == pd.Timestamp(ts).date())
    except Exception:                                           # noqa: BLE001
        pass
    return out


# ─── per candle (called from pe_ce_panel.table_rows) ──────────────────────────
def add(t: pd.DataFrame, s: pd.DataFrame, tf_min: int) -> pd.DataFrame:
    if t.empty:
        return t
    cols = [c for c in ("pe_wall", "ce_wall", "exp_day") if c in s.columns]
    if cols:
        w = s.set_index("ts")[cols].resample(f"{tf_min}min", origin="start_day", offset="15min").last()
        t = t.drop(columns=[c for c in cols if c in t.columns]).merge(w.reset_index(), on="ts", how="left")
        for c in ("pe_wall", "ce_wall"):
            if c in t:
                t[c] = t[c].ffill()
    t["pro"] = pro_reads(t, tf_min)
    return t


def pro_reads(t: pd.DataFrame, tf_min: int) -> list[tuple[str, str, str, str]]:
    """(text, kind 'bull'|'bear'|'flat'|'skip', strength dots, wall line) per row."""
    L = max(5, int(round(30 / max(tf_min, 1))))                # ~30 min look-back
    K = max(3, int(round(15 / max(tf_min, 1))))                # ~15 min trend
    a = t["pe_ce_chg"].abs()
    prior = a.shift(1)
    med = prior.expanding(min_periods=3).median()
    avg = prior.expanding(min_periods=3).mean()
    base = pd.concat([med, 0.5 * avg], axis=1).max(axis=1)        # median, but not below half the mean (zero-heavy days)
    base = base.fillna(a.expanding().mean()).replace(0, np.nan)
    pxa = t["px_chg"].abs()
    pxbase = pxa.shift(1).expanding(min_periods=3).median().fillna(pxa.expanding().median())
    out = []
    for i in range(len(t)):
        r = t.iloc[i]
        tm = r.ts.time()
        mult = float(a.iloc[i] / base.iloc[i]) if pd.notna(base.iloc[i]) and base.iloc[i] else 1.0
        dots = "●●●" if mult >= 3 else "●●" if mult >= 1.5 else "●" if mult >= 0.7 else "·"
        wall = _walls(t, i)
        if tm >= CLOSE:
            out.append(("After close — frozen spot / closing-price print, ignore", "skip", "", wall)); continue
        if bool(r.get("exp_day", False)) and tm >= UNWIND_FROM and i >= K \
                and r.pe_day < t["pe_day"].iloc[i - K] and r.ce_day < t["ce_day"].iloc[i - K]:
            out.append(("Expiry unwind — both sides closing, not direction", "skip", dots, wall)); continue
        lo = max(0, i - L)
        prev = t.iloc[lo:i]
        rng = float(t["pe_ce_day"].iloc[: i + 1].max() - t["pe_ce_day"].iloc[: i + 1].min()) or 1.0
        if len(prev) >= 3:
            if r.close > prev.close.max() and r.pe_ce_day < prev.pe_ce_day.max() - 0.10 * rng:
                out.append(("⚠ Bearish divergence — price new high, writers NOT following", "bear", dots, wall)); continue
            if r.close < prev.close.min() and r.pe_ce_day > prev.pe_ce_day.min() + 0.10 * rng:
                out.append(("⚠ Bullish divergence — price new low, writers NOT following", "bull", dots, wall)); continue
        if dots == "·":
            out.append(("Noise — too small to read", "flat", dots, wall)); continue
        if mult >= 1.5 and pd.notna(pxbase.iloc[i]) and pxa.iloc[i] <= 0.25 * max(pxbase.iloc[i], 0.5):
            side = "put writers" if r.pe_ce_chg > 0 else "call writers"
            k = "bull" if r.pe_ce_chg > 0 else "bear"
            out.append((f"Absorption — {side} active, price stuck", k, dots, wall)); continue
        if i >= K:
            w_tr = float(r.pe_ce_day - t["pe_ce_day"].iloc[i - K])
            p_tr = float(r.close - t["close"].iloc[i - K])
            cand = r.read[1] if isinstance(r.read, tuple) else "flat"
            if w_tr > 0 and p_tr > 0:
                if cand == "bear":
                    out.append(("Pause inside an UP trend (writers still bullish)", "bull", dots, wall)); continue
                out.append(("Confirmed ▲ — writers + price agree", "bull", dots, wall)); continue
            if w_tr < 0 and p_tr < 0:
                if cand == "bull":
                    out.append(("Pause inside a DOWN trend (writers still bearish)", "bear", dots, wall)); continue
                out.append(("Confirmed ▼ — writers + price agree", "bear", dots, wall)); continue
            if w_tr > 0 > p_tr:
                out.append(("Writers bullish, price falling — dip being bought?", "bull", dots, wall)); continue
            if w_tr < 0 < p_tr:
                out.append(("Writers bearish, price rising — rally being sold?", "bear", dots, wall)); continue
        out.append(("No extra context", "flat", dots, wall))
    return out


def _walls(t: pd.DataFrame, i: int) -> str:
    if i == 0 or "pe_wall" not in t:
        return ""
    r, p = t.iloc[i], t.iloc[i - 1]
    bits = []
    if pd.notna(r.pe_wall) and pd.notna(p.pe_wall) and r.pe_wall != p.pe_wall:
        bits.append(f"Floor {'↑' if r.pe_wall > p.pe_wall else '↓'} {p.pe_wall:,.0f}→{r.pe_wall:,.0f}")
    if pd.notna(r.ce_wall) and pd.notna(p.ce_wall) and r.ce_wall != p.ce_wall:
        bits.append(f"Ceiling {'↑' if r.ce_wall > p.ce_wall else '↓'} {p.ce_wall:,.0f}→{r.ce_wall:,.0f}")
    return " · ".join(bits)


# ─── HTML cell (called from pe_ce_panel.render_table) ─────────────────────────
_BG = {"bull": "#dcfce7", "bear": "#fee2e2", "skip": "#f1f5f9", "flat": ""}
_FG = {"bull": "#15803d", "bear": "#b91c1c", "skip": "#64748b", "flat": "#64748b"}


def cell(r) -> str:
    p = getattr(r, "pro", None)
    if not isinstance(p, tuple):
        return "<td style='border-bottom:1px solid #e2e8f0'></td>"
    txt, kind, dots, wall = p
    strong = kind in ("bull", "bear") and dots in ("●●", "●●●")
    w = (f"<div style='font-size:0.75rem;color:#7e22ce;font-weight:600'>{html.escape(wall)}</div>" if wall else "")
    d = f"<span style='color:#a16207;letter-spacing:1px;margin-right:6px'>{dots}</span>" if dots else ""
    return (f"<td style='background:{_BG.get(kind, '')};color:{_FG.get(kind, '#334155')};text-align:left;padding:5px 8px;"
            f"border-bottom:1px solid #e2e8f0;white-space:normal;min-width:260px;font-size:0.8rem;"
            f"{'font-weight:700;' if strong else ''}'>{d}{html.escape(txt)}{w}</td>")


GUIDE = ["<b>★ PRO READ</b> adds the context the Signal column can't see from one candle:",
         "<b>Dots</b> = size vs a normal candle today (●●● ≥ 3×, ●● ≥ 1.5×, ● normal, · noise). Act only on ●● / ●●●.",
         "<b>Grey rows</b> = not tradeable information: after 15:30 (spot frozen, closing-price print) and expiry-afternoon "
         "unwinding (both sides closing).",
         "<b>⚠ Divergence</b> = price made a new ~30-min high/low but PE − CE (Day) did not follow. The most useful read — "
         "a rally without fresh put writing is running on empty.",
         "<b>Confirmed ▲/▼</b> = PE − CE (Day) trend and price agree over ~15 min. A Signal against it = usually a pause.",
         "<b>Absorption</b> = a big writer candle with no price move: writers are soaking up buyers/sellers at that level.",
         "<b>Floor ↑ / Ceiling ↓</b> (purple) = the biggest put / call strike moved — writers re-positioning. Floor moving UP "
         "under price is bullish; ceiling moving DOWN onto price is bearish.",
         "None of these tags is proven yet — run <code>backtest_pece_pro.py</code> on your stored days and trust only the "
         "tags that show an edge there."]
