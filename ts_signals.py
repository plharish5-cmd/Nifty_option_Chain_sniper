"""
ts_signals.py — BUY / SELL level signals for the ⏱ Time-series Chain grid (ts_chain_pro.py).

Turns the strike × time OI grid into the questions a trader actually asks, one answer per time bucket, using only
data up to that bucket (no look-ahead):

  🟢 SUPPORT BUILT        put OI at the support strike up ≥ 10 % over ~15 min
  🔴 RESISTANCE BUILT     call OI at the resistance strike up ≥ 10 %
  ⬆ RESISTANCE BROKEN    2 closes ≥ 20 pts above the old ceiling + its calls covering + with the day trend → BUY CE
  ⬇ SUPPORT BROKEN       2 closes ≥ 20 pts below the old floor + its puts unwinding + with the day trend → BUY PE
  🔼 REVERSAL FROM SUPPORT     bucket low tagged the floor, closed back ≥ 15 pts above it, puts there still being
                               written (defended) → BUY CE, SL below the floor
  🔽 REVERSAL FROM RESISTANCE  bucket high tagged the ceiling, closed back ≥ 15 pts below, calls still being written
                               → BUY PE, SL above the ceiling
  ⚠ FAKE BREAKOUT        closed beyond a wall but the writers there kept ADDING, or the break failed back inside

Support / resistance / break logic is the SAME engine as the 🎯 LEVELS column of the PE − CE table
(levels_read.py), so both pages always agree. Not backtested for reversals yet — information, not a trigger.
"""
from __future__ import annotations

from datetime import time

import numpy as np
import pandas as pd

import levels_read as lr

TOUCH = 0.3        # × strike step: a bucket low/high within this of the wall = "tagged" it
RECLAIM = 0.3      # × strike step: and closed at least this far back inside
NO_ENTRY = time(15, 0)


def _bucket_of(x: pd.DataFrame, minutes: int) -> pd.Series:
    d0 = x.ts.min().normalize() + pd.Timedelta(hours=9, minutes=15)
    step = pd.Timedelta(minutes=minutes)
    k = np.ceil(((x.ts.dt.floor("min") - d0) / step).astype(float)).clip(lower=0)
    return d0 + k.astype(int) * step


def compute(x: pd.DataFrame, minutes: int, end: pd.Timestamp | None = None) -> pd.DataFrame:
    """One row per bucket END: close, low, high, sup, res, ev (text, kind, tag), act (text, kind), badge."""
    if x is None or x.empty:
        return pd.DataFrame()
    if end is not None:
        x = x[x.ts <= end]
    x = x.assign(b=_bucket_of(x, minutes))
    px = x.drop_duplicates(["ts"]).groupby("b")["spot"].agg(low="min", high="max", close="last")
    last = x.groupby("b")["ts"].transform("max")
    y = x[x.ts == last]
    rows = []
    for b, g in y.groupby("b", sort=True):
        k = g["strike"].astype(int).values
        pe = dict(zip(k, pd.to_numeric(g["pe_oi"], errors="coerce").fillna(0).astype(float)))
        ce = dict(zip(k, pd.to_numeric(g["ce_oi"], errors="coerce").fillna(0).astype(float)))
        dchg = (pd.to_numeric(g["pe_dchg"], errors="coerce").fillna(0).sum()
                - pd.to_numeric(g["ce_dchg"], errors="coerce").fillna(0).sum())
        rows.append(dict(ts=pd.Timestamp(b), pe_map=pe, ce_map=ce, pe_ce_day=float(dchg)))
    t = pd.DataFrame(rows).merge(px.reset_index().rename(columns={"b": "ts"}), on="ts", how="left")
    if t.empty:
        return t
    if not np.isfinite(t["pe_ce_day"]).any() or not t["pe_ce_day"].abs().sum():           # no change column → vs first
        tot = [sum(r.pe_map.values()) - sum(r.ce_map.values()) for r in t.itertuples()]
        t["pe_ce_day"] = np.array(tot) - tot[0]
    t = lr.annotate(t, minutes)
    _reversals(t, minutes)
    t["badge"] = [_badge(e) for e in t["lv"]]
    return t


def _reversals(t: pd.DataFrame, minutes: int) -> None:
    """Overwrite quiet buckets with reversal / fake-out reads where the wall was tagged and defended."""
    K = max(1, int(round(15 / max(minutes, 1))))
    for i in range(1, len(t)):
        r, p = t.iloc[i], t.iloc[i - 1]
        ev = r.lv if isinstance(r.lv, tuple) else ("", "flat", "")
        if ev[2] in ("break_up", "break_dn", "fail", "fake") or r.ts.time() < lr.WARMUP or r.ts.time() >= time(15, 30):
            continue
        pe, ce = r.pe_map, r.ce_map
        if not isinstance(pe, dict) or not pe:
            continue
        step = lr._step(pe)
        S, R = p.sup, p.res                                    # the walls as they stood BEFORE this bucket
        j = max(0, i - K)
        pe0, ce0 = t.iloc[j].pe_map, t.iloc[j].ce_map
        if (S is not None and np.isfinite(r.low) and r.low <= S + TOUCH * step and r.close >= S + RECLAIM * step
                and r.close - r.low >= RECLAIM * step and r.close > p.close):      # real bounce off the floor
            d = lr._chg(pe, pe0, S)
            if d is not None and d > 0:
                t.at[t.index[i], "lv"] = (f"🔼 REVERSAL FROM SUPPORT {S:,.0f} — low {r.low:,.0f} tagged it, closed back "
                                           f"{r.close - S:+.0f} pts, puts defended ({d:+.0%})", "bull", "rev_up")
                if not r.lv_state:
                    t.at[t.index[i], "act"] = ((f"BUY CE · SL below {S:,.0f}" if r.ts.time() < NO_ENTRY
                                                else "No fresh entry after 15:00"), "bull")
                continue
        if (R is not None and np.isfinite(r.high) and r.high >= R - TOUCH * step and r.close <= R - RECLAIM * step
                and r.high - r.close >= RECLAIM * step and r.close < p.close):     # real rejection off the ceiling
            d = lr._chg(ce, ce0, R)
            if d is not None and d > 0:
                t.at[t.index[i], "lv"] = (f"🔽 REVERSAL FROM RESISTANCE {R:,.0f} — high {r.high:,.0f} tagged it, closed back "
                                           f"{r.close - R:+.0f} pts, calls defended ({d:+.0%})", "bear", "rev_dn")
                if not r.lv_state:
                    t.at[t.index[i], "act"] = ((f"BUY PE · SL above {R:,.0f}" if r.ts.time() < NO_ENTRY
                                                else "No fresh entry after 15:00"), "bear")


def _badge(ev) -> tuple[str, str]:
    """Short label for the grid's signal row → (text, kind)."""
    if not isinstance(ev, tuple):
        return "", "flat"
    txt, kind, tag = ev
    import re
    num = re.search(r"(\d{2},\d{3})", txt)
    lv = num.group(1).replace(",", "") if num else ""
    m = {"break_up": f"⬆ R {lv} broken", "break_dn": f"⬇ S {lv} broken", "rev_up": f"🔼 rev ↑ {lv}",
         "rev_dn": f"🔽 rev ↓ {lv}", "fake": f"⚠ fake {lv}", "fail": f"✖ failed {lv}", "s_build": f"🟢 S {lv} built",
         "r_build": f"🔴 R {lv} built", "test": f"testing {lv}", "against": f"vs trend {lv}"}
    if tag in m:
        return m[tag], ("bull" if tag in ("break_up", "rev_up", "s_build") else
                        "bear" if tag in ("break_dn", "rev_dn", "r_build") else "warn")
    if "weakening" in txt:
        return ("🟠 S weak" if "Support" in txt else "🟠 R weak"), "warn"
    return "", "flat"


_BG = {"bull": "#dcfce7", "bear": "#fee2e2", "warn": "#fef3c7", "flat": "#ffffff"}
_FG = {"bull": "#15803d", "bear": "#b91c1c", "warn": "#92400e", "flat": "#94a3b8"}


def badge_cell(b: tuple[str, str], th: str) -> str:
    txt, kind = b if isinstance(b, tuple) else ("", "flat")
    strong = any(s in txt for s in ("broken", "rev"))
    return (f"<th style='{th};background:{_BG.get(kind, '#fff')};color:{_FG.get(kind, '#94a3b8')};font-size:10.5px;"
            f"{'font-weight:800;' if strong else 'font-weight:600;'}white-space:nowrap'>{txt or '·'}</th>")


def card_html(t: pd.DataFrame, cols: list, labels: dict, WHITE: str, MUTED: str, GOLD: str, GREEN: str, RED: str) -> str:
    """Now-line + every signal event of the session (newest first)."""
    if t is None or t.empty:
        return ""
    last = t.iloc[-1]
    a = last.act if isinstance(last.act, tuple) else ("WAIT", "flat")
    ac = GREEN if a[1] == "bull" else RED if a[1] == "bear" else GOLD
    s, r = last.sup, last.res
    head = (f"<div style='font-size:1.02rem;margin-bottom:6px'>NOW <b style='color:{ac}'>{a[0]}</b>"
            f" &nbsp;·&nbsp; Support <b style='color:{GREEN}'>{'' if s is None else f'{s:,.0f}'}</b>"
            f" &nbsp;·&nbsp; Resistance <b style='color:{RED}'>{'' if r is None else f'{r:,.0f}'}</b>"
            f" &nbsp;·&nbsp; NIFTY {last.close:,.1f}</div>")
    keep = ("break_up", "break_dn", "rev_up", "rev_dn", "fake", "fail", "s_build", "r_build", "against")
    ev = t[[isinstance(e, tuple) and e[2] in keep for e in t["lv"]]].iloc[::-1]
    # collapse repeats: show a "built" read only when it first appears / the level changes
    rows, seen = [], set()
    for x in ev.itertuples():
        key = (x.lv[2], x.sup if x.lv[2] == "s_build" else x.res if x.lv[2] == "r_build" else x.ts)
        if key in seen:
            continue
        seen.add(key)
        c = GREEN if x.lv[1] == "bull" else RED if x.lv[1] == "bear" else GOLD
        act = x.act[0] if isinstance(x.act, tuple) else ""
        rows.append(f"<tr><td style='padding:3px 8px;color:{GOLD};white-space:nowrap'><b>{x.ts:%H:%M}</b></td>"
                    f"<td style='padding:3px 8px;white-space:nowrap'>{x.close:,.0f}</td>"
                    f"<td style='padding:3px 8px;color:{c}'>{x.lv[0]}</td>"
                    f"<td style='padding:3px 8px;white-space:nowrap;color:{c}'><b>{act if act.startswith('BUY') else ''}</b></td></tr>")
        if len(rows) >= 14:
            break
    tbl = (f"<div style='overflow-x:auto'><table style='color:{WHITE};font-size:0.86rem;width:100%'>"
           f"<tr style='color:{MUTED};text-align:left'><th style='padding:3px 8px'>Time</th><th style='padding:3px 8px'>NIFTY</th>"
           f"<th style='padding:3px 8px'>Signal</th><th style='padding:3px 8px'>Trade</th></tr>{''.join(rows)}</table></div>"
           if rows else f"<div style='color:{MUTED}'>No level signals yet today.</div>")
    note = (f"<div style='color:{MUTED};font-size:0.78rem;margin-top:6px'>Breaks: 2 closes ≥ 20 pts beyond the wall, writers "
            "there exiting, with the day trend (backtested: an index edge, NOT yet profitable as option buys — treat as "
            "information). Reversals: wall tagged and defended — not backtested yet. Same engine as the 🎯 LEVELS column.</div>")
    return head + tbl + note
