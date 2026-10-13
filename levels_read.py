"""
levels_read.py — SUPPORT / RESISTANCE read for the PE − CE OI table and the Smart OI charts.

The old READ column labels ONE candle's total PE − CE change. Traders don't trade totals, they trade LEVELS:
"where is the floor, is it getting stronger, did price break it, did the writers run?"  This module answers that for
every candle, using only data up to that candle (no look-ahead).

  Support    = strike at/below price with the most PUT OI (within ±8 strikes)
  Resistance = strike at/above price with the most CALL OI
  For each candle, against ~15 min earlier:

  🟢 SUPPORT BEING BUILT      put OI at the support strike up ≥ 10 %             → buy CE on dips to it
  🔴 RESISTANCE BEING BUILT   call OI at the resistance strike up ≥ 10 %         → buy PE on rises to it
  🟠 SUPPORT WEAKENING        put OI at support down ≥ 10 % (writers leaving)   → don't buy dips
  🟠 RESISTANCE WEAKENING     call OI at resistance down ≥ 10 % (calls covering)→ breakout getting likely
  ⬆ RESISTANCE BROKEN         2 closes above the old resistance AND its call OI falling (covering) → BUY CE
  ⬇ SUPPORT BROKEN            2 closes below the old support AND its put OI falling (unwinding)    → BUY PE
  ⚠ FAKE BREAK RISK           closed beyond, but writers there are still ADDING → wait
  ✖ BREAK FAILED              price back inside the old level → exit the break trade
  ↑ / ↓ shifts                support moved up / resistance moved down without a break (writers rolling)

Expiry-day unwind is reported only when BOTH walls are thinning AND price is not breaking anything.
Information only — not financial advice. Not backtested yet: run backtest_pece_pro.py before trusting a tag.
"""
from __future__ import annotations

from datetime import time

import numpy as np
import pandas as pd

BUILD = 0.10          # ≥ 10 % OI change at the level over the look-back = building / weakening
BREAK_BUF = 0.4       # a close must be ≥ 0.4 × strike step (20 pts on NIFTY) beyond the wall to count as a break
TREND_FILTER = True   # take a break only WITH the day trend (price vs session average + PE−CE 30-min slope)
TRAIL = False         # trailing stop: raise SL to each new support built under price (set by backtest verdict)
WARMUP = time(9, 30)  # no building / weakening reads before this (first candles compare against the open)
NEAR = 8              # strikes each side of price searched for walls
NO_ENTRY = time(15, 0)
CLOSE = time(15, 30)


# ─── per snapshot: OI by strike (called from pece_pro.extra) ─────────────────
def maps(d: pd.DataFrame) -> dict:
    try:
        k = pd.to_numeric(d["strike"], errors="coerce").round()
        pe = pd.to_numeric(d["pe_oi"], errors="coerce").fillna(0)
        ce = pd.to_numeric(d["ce_oi"], errors="coerce").fillna(0)
        ok = k.notna()
        return {"pe_map": dict(zip(k[ok].astype(int), pe[ok].astype(float))),
                "ce_map": dict(zip(k[ok].astype(int), ce[ok].astype(float)))}
    except Exception:                                           # noqa: BLE001
        return {"pe_map": None, "ce_map": None}


def _step(m: dict) -> float:
    ks = sorted(m)
    if len(ks) < 2:
        return 50.0
    d = np.diff(ks)
    d = d[d > 0]
    return float(d.min()) if len(d) else 50.0


def _walls(pe: dict, ce: dict, px: float, step: float):
    lo, hi = px - NEAR * step, px + NEAR * step
    sup = [(v, k) for k, v in pe.items() if lo <= k <= px + 0.1 * step and v > 0]
    res = [(v, k) for k, v in ce.items() if px - 0.1 * step <= k <= hi and v > 0]
    return (max(sup)[1] if sup else None), (max(res)[1] if res else None)


def _chg(now: dict, old: dict, k) -> float | None:
    if k is None or not now or not old:
        return None
    a, b = now.get(k), old.get(k)
    if a is None or b is None or b <= 0:
        return None
    return a / b - 1.0


def _with_trend(t: pd.DataFrame, i: int, side: str, tf_min: int) -> bool:
    """Day trend: price above (below) the session average AND PE − CE (day) rising (falling) over ~30 min."""
    try:
        c = float(t["close"].iloc[i])
        twap = float(t["close"].iloc[: i + 1].mean())
        k = max(1, int(round(30 / max(tf_min, 1))))
        slope = float(t["pe_ce_day"].iloc[i] - t["pe_ce_day"].iloc[max(0, i - k)])
    except Exception:                                           # noqa: BLE001
        return True
    return (c > twap and slope > 0) if side == "UP" else (c < twap and slope < 0)


def annotate(t: pd.DataFrame, tf_min: int) -> pd.DataFrame:
    """Adds sup, res, lv (event text, kind, marker) and act (action text, kind) per candle."""
    t = t.copy()
    n = len(t)
    sup, res, lv, act, state_col = [None] * n, [None] * n, [None] * n, [None] * n, [""] * n
    if "pe_map" not in t or n == 0:
        t["sup"], t["res"], t["lv"], t["act"], t["lv_state"] = sup, res, lv, act, state_col
        return t
    K = max(2, int(round(15 / max(tf_min, 1))))
    state, lvl = "", None                   # "UP"/"DN" break trade active and its level
    stop = None                             # current stop level (= lvl, or trailed)
    pend = None                             # ("UP"/"DN", level) first close beyond, awaiting acceptance
    prev_s = prev_r = None
    step = 50.0
    for i in range(n):
        r = t.iloc[i]
        pe, ce = r.get("pe_map"), r.get("ce_map")
        if not isinstance(pe, dict) or not isinstance(ce, dict) or not pe:
            lv[i] = ("—", "flat", "")                  # missing snapshot: keep the open trade, don't exit
            act[i] = act[i - 1] if i and act[i - 1] else ("—", "flat")
            state_col[i] = state
            continue
        step = _step(pe)
        buf = BREAK_BUF * step
        c = float(r.close)
        tm = r.ts.time()
        s, rz = _walls(pe, ce, c, step)
        sup[i], res[i] = s, rz
        j = max(0, i - K)
        pe0, ce0 = t.iloc[j].get("pe_map"), t.iloc[j].get("ce_map")
        pe0 = pe0 if isinstance(pe0, dict) else {}
        ce0 = ce0 if isinstance(ce0, dict) else {}
        ev = None

        # 1 · break confirmation (2nd close beyond the old level)
        if pend:
            side, L = pend
            if side == "UP" and c > L + buf:
                d = _chg(ce, ce0, L)
                if d is not None and d > 0.02:
                    ev = (f"⚠ Above {L:,.0f} but calls still being WRITTEN there ({d:+.0%}) — fake-break risk", "flat", "fake")
                elif TREND_FILTER and not _with_trend(t, i, "UP", tf_min):
                    ev = (f"⬆ {L:,.0f} broken but AGAINST the day trend — no trade", "flat", "against")
                else:
                    ev = (f"⬆ RESISTANCE {L:,.0f} BROKEN — closes held above, calls covering"
                          + (f" ({d:+.0%})" if d is not None else ""), "bull", "break_up")
                    state, lvl, stop = "UP", L, L
            elif side == "DN" and c < L - buf:
                d = _chg(pe, pe0, L)
                if d is not None and d > 0.02:
                    ev = (f"⚠ Below {L:,.0f} but puts still being WRITTEN there ({d:+.0%}) — fake-break risk", "flat", "fake")
                elif TREND_FILTER and not _with_trend(t, i, "DN", tf_min):
                    ev = (f"⬇ {L:,.0f} broken but AGAINST the day trend — no trade", "flat", "against")
                else:
                    ev = (f"⬇ SUPPORT {L:,.0f} BROKEN — closes held below, puts unwinding"
                          + (f" ({d:+.0%})" if d is not None else ""), "bear", "break_dn")
                    state, lvl, stop = "DN", L, L
            pend = None
        # 2 · first close beyond the previous candle's wall
        if ev is None and tm >= WARMUP:
            if prev_r is not None and c > prev_r + buf and state != "UP":
                pend = ("UP", prev_r)
                ev = (f"Testing break above {prev_r:,.0f} — needs one more close above", "flat", "test")
            elif prev_s is not None and c < prev_s - buf and state != "DN":
                pend = ("DN", prev_s)
                ev = (f"Testing break below {prev_s:,.0f} — needs one more close below", "flat", "test")
        # 3 · failed break
        s_now, r_now = _walls(pe, ce, c, step)
        if TRAIL and state == "UP" and s_now is not None and stop is not None and stop < s_now < c - buf:
            stop = s_now                                    # new support built under price → raise SL
        if TRAIL and state == "DN" and r_now is not None and stop is not None and c + buf < r_now < stop:
            stop = r_now
        if state == "UP" and stop is not None and c < stop - buf:
            ev = ((f"✖ Break above {lvl:,.0f} FAILED — back inside" if stop == lvl
                   else f"✖ Trailing stop {stop:,.0f} hit — exit CE"), "bear", "fail")
            state, lvl, stop = "", None, None
        elif state == "DN" and stop is not None and c > stop + buf:
            ev = ((f"✖ Break below {lvl:,.0f} FAILED — back inside" if stop == lvl
                   else f"✖ Trailing stop {stop:,.0f} hit — exit PE"), "bull", "fail")
            state, lvl, stop = "", None, None
        # 4 · building / weakening at the current walls
        ds, dr = _chg(pe, pe0, s), _chg(ce, ce0, rz)
        if ev is None and tm < WARMUP:
            ev = ("Warm-up — walls still forming", "flat", "")
        if ev is None:
            sb, rb = ds is not None and ds >= BUILD, dr is not None and dr >= BUILD
            sw, rw = ds is not None and ds <= -BUILD, dr is not None and dr <= -BUILD
            if sw and rw:
                ev = (f"Both walls thinning ({s:,.0f} puts {ds:+.0%}, {rz:,.0f} calls {dr:+.0%})"
                      + (" — expiry unwind, not a direction" if bool(r.get("exp_day", False)) else " — breakout pending"),
                      "skip" if bool(r.get("exp_day", False)) else "flat", "")
            elif sb and rb:
                ev = (f"Range being built {s:,.0f}–{rz:,.0f} (both sides writing)", "flat", "")
            elif sb:
                ev = (f"🟢 SUPPORT being built at {s:,.0f} (puts {ds:+.0%})", "bull", "s_build")
            elif rb:
                ev = (f"🔴 RESISTANCE being built at {rz:,.0f} (calls {dr:+.0%})", "bear", "r_build")
            elif rw:
                ev = (f"🟠 Resistance {rz:,.0f} weakening (calls {dr:+.0%}) — breakout up getting likely", "bull", "")
            elif sw:
                ev = (f"🟠 Support {s:,.0f} weakening (puts {ds:+.0%}) — don't buy dips", "bear", "")
            elif prev_s is not None and s is not None and s > prev_s:
                ev = (f"↑ Support shifted up {prev_s:,.0f}→{s:,.0f}", "bull", "")
            elif prev_r is not None and rz is not None and rz < prev_r:
                ev = (f"↓ Resistance shifted down {prev_r:,.0f}→{rz:,.0f}", "bear", "")
            else:
                ev = (f"Holding {s:,.0f}–{rz:,.0f}" if s and rz else "—", "flat", "")
        lv[i] = ev

        # 5 · action
        if tm >= CLOSE:
            a = ("After close — ignore", "skip")
        elif state == "UP":
            a = ((f"Hold/trail CE · no fresh entry after 15:00" if tm >= NO_ENTRY else f"BUY CE · SL below {stop:,.0f}"), "bull")
        elif state == "DN":
            a = ((f"Hold/trail PE · no fresh entry after 15:00" if tm >= NO_ENTRY else f"BUY PE · SL above {stop:,.0f}"), "bear")
        elif tm >= NO_ENTRY:
            a = ("No fresh entry after 15:00", "skip")
        elif tm < WARMUP:
            a = ("WAIT (warm-up)", "flat")
        elif ev[2] == "s_build" or (ds is not None and ds >= BUILD and not (dr is not None and dr >= BUILD)):
            a = (f"CE on dip near {s:,.0f} · SL below it", "bull")
        elif ev[2] == "r_build" or (dr is not None and dr >= BUILD and not (ds is not None and ds >= BUILD)):
            a = (f"PE on rise near {rz:,.0f} · SL above it", "bear")
        else:
            a = ("WAIT", "flat")
        act[i] = a
        state_col[i] = state
        # keep the OLD wall while price sits just beyond it (inside the break buffer) — otherwise the wall 'moves' with
        # price and a slow grind through it is never seen as a break
        keep_r = prev_r is not None and prev_r < c <= prev_r + buf
        keep_s = prev_s is not None and prev_s - buf <= c < prev_s
        prev_s = prev_s if keep_s else s
        prev_r = prev_r if keep_r else rz
    t["sup"], t["res"], t["lv"], t["act"], t["lv_state"] = sup, res, lv, act, state_col
    return t


# ─── HTML cells (called from pe_ce_panel.render_table) ───────────────────────
_BG = {"bull": "#dcfce7", "bear": "#fee2e2", "skip": "#f1f5f9", "flat": ""}
_FG = {"bull": "#15803d", "bear": "#b91c1c", "skip": "#64748b", "flat": "#334155"}


def cells(r) -> str:
    td = "border-bottom:1px solid #e2e8f0;padding:5px 8px;"
    s, rz = getattr(r, "sup", None), getattr(r, "res", None)
    out = (f"<td style='{td}color:#15803d;font-weight:700;text-align:center'>{'' if s is None else f'{s:,.0f}'}</td>"
           f"<td style='{td}color:#b91c1c;font-weight:700;text-align:center'>{'' if rz is None else f'{rz:,.0f}'}</td>")
    ev = getattr(r, "lv", None)
    if isinstance(ev, tuple):
        big = ev[2] in ("break_up", "break_dn", "fail")
        out += (f"<td style='{td}background:{_BG.get(ev[1], '')};color:{_FG.get(ev[1], '#334155')};text-align:left;"
                f"white-space:normal;min-width:250px;font-size:0.8rem;{'font-weight:800;' if big else ''}'>{ev[0]}</td>")
    else:
        out += f"<td style='{td}'></td>"
    a = getattr(r, "act", None)
    if isinstance(a, tuple):
        solid = a[0].startswith("BUY")
        bg = ({"bull": "#16a34a", "bear": "#dc2626"}.get(a[1], "") if solid else _BG.get(a[1], ""))
        fg = "#fff" if solid else _FG.get(a[1], "#334155")
        out += (f"<td style='{td}background:{bg};color:{fg};font-weight:700;white-space:nowrap;"
                f"text-align:left'>{a[0]}</td>")
    else:
        out += f"<td style='{td}'></td>"
    return out


GUIDE = ["<b>🎯 LEVELS · ACTION</b> — read this group first. It answers: where is the floor / ceiling, is it getting stronger, "
         "did price break it, and did the writers run?",
         "<b>Support</b> = strike at/below price with the most put OI. <b>Resistance</b> = strike at/above price with the most call OI.",
         "<b>🟢 Support being built / 🔴 Resistance being built</b> = OI at that strike up ≥ 10 % in ~15 min. "
         "Trade toward it: CE on a dip to a building support, PE on a rise to a building resistance.",
         "<b>🟠 Weakening</b> = writers at that wall leaving (≥ 10 % down). Weak resistance → breakout up likely; "
         "weak support → don't buy dips.",
         "<b>Testing break</b> = one close beyond the old wall. <b>⬆ / ⬇ BROKEN</b> = a second close beyond AND the writers "
         "there are exiting (covering / unwinding) → that's the strongest signal on this page: BUY CE / BUY PE with SL on the "
         "other side of the broken level. <b>⚠ fake-break risk</b> = closed beyond but writers are still adding there → wait.",
         "<b>✖ Break failed</b> = back inside the old level → exit the break trade.",
         "After 15:00: no fresh entries, only manage open trades. Not backtested yet — check backtest_pece_pro.py."]
