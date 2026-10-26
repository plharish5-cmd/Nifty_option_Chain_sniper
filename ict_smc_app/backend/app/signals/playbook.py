"""
signals/playbook.py — the three setups, evaluated on CLOSED candles of the primary timeframe (5m) with the
bias timeframe (15m) as a filter.

A  LIQUIDITY RAID → MSS → FVG RETEST   (the core ICT model; "turtle soup + MSS")
   1. A sweep (or false breakout) of a liquidity pool: BSL for a short, SSL for a long.
   2. The raid is CANCELLED if any later candle closes back beyond the raided level before the entry.
   3. Within sweep_to_mss_max_bars, structure breaks the other way (MSS / BOS) WITH displacement.
   4. The displacement leg left an FVG (formed after the raid, at most one bar after the MSS).
   5. Within mss_to_entry_max_bars after the MSS, a candle trades back into the FVG and CLOSES inside or
      beyond it in the trade direction → entry at that close.
   Filters: entry in premium (short) / discount (long) of the raid-to-extreme range, bias-timeframe trend
   not against the trade, inside a trade window, risk between min_risk_pts and max_risk_pts,
   reward : risk to the nearest opposing liquidity (known at entry) ≥ min_rr.
   Stop = the raid's extreme (highest high since the sweep for a short) + sl_buffer_pts.

B  BOS / MSS CONFIRMATION (info) — a structure break with displacement just closed.

C  OTE TOUCH — after a structure break, the first closed candle that retraces into the 61.8–79 % band of the
   dealing range and closes inside it (not through the origin). Stop beyond the range origin, target the range
   extreme (the liquidity the leg left behind). Same filters as A.

Every evaluation returns TRADE or SKIP with the reason, so the app can show "Setup A skipped: R:R 0.9 < 1.5"
instead of staying silent — you learn more from the skips than from the trades.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np

from .. import config
from ..engine.analyze import Analysis
from ..engine.liquidity import untaken_at
from ..timeutil import TF_SECONDS, hm, in_windows, parse_hm, to_ist


@dataclass
class Signal:
    key: str
    setup: str               # "A" | "B" | "C"
    name: str
    tf: str
    side: str                # "long" | "short"
    i: int                   # bar index of the trigger candle (entry candle for A/C)
    ts: int                  # its start time (epoch)
    status: str              # "TRADE" | "SKIP" | "INFO"
    entry: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    risk: Optional[float] = None
    rr: Optional[float] = None
    target_src: str = ""
    reasons: List[str] = field(default_factory=list)
    story: List[str] = field(default_factory=list)
    confidence: Optional[float] = None          # set by delta/confluence.py for A/C only; never changes status
    confluence: List[str] = field(default_factory=list)
    ctx: Dict[str, Any] = field(default_factory=dict)
    confidence_parts: Optional[Dict[str, Any]] = None      # structure / options_context / flow (+ flow_source)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["time"] = hm(self.ts)
        return d

    @property
    def headline(self) -> str:
        arrow = "▲ LONG" if self.side == "long" else "▼ SHORT"
        if self.setup == "B":
            return f"{self.tf} {self.name}"
        core = f"{self.name} · {arrow} {self.tf} @ {self.entry:,.1f}"
        if self.status == "TRADE":
            return f"{core} · SL {self.sl:,.1f} · TP {self.tp:,.1f} ({self.target_src}) · R:R {self.rr:.1f}"
        return f"{core} · skipped: {'; '.join(self.reasons)}"


def _close_time(an: Analysis, i: int) -> int:
    return int(an.bars.ts[i]) + TF_SECONDS[an.tf]


def _filters(an: Analysis, bias: Optional[Analysis], i: int, side: str, entry: float, sl: float,
             target: Optional[float], tsrc: str, pd_ok: Optional[bool],
             ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    P = config.rules()["playbook"]
    S = config.rules()["session"]
    reasons = []
    risk = abs(entry - sl)
    if target is None:
        target, tsrc = (entry - 2 * risk if side == "short" else entry + 2 * risk), "2R (no liquidity known)"
    rr = abs(target - entry) / risk if risk > 0 else 0.0
    if risk < float(P["min_risk_pts"]):
        reasons.append(f"risk {risk:.1f} < {P['min_risk_pts']} pts (stop too tight for NIFTY noise)")
    if risk > float(P["max_risk_pts"]):
        reasons.append(f"risk {risk:.1f} > {P['max_risk_pts']} pts")
    if rr < float(P["min_rr"]):
        reasons.append(f"R:R {rr:.1f} < {P['min_rr']} to {tsrc}")
    t_close = _close_time(an, i)
    ist_close = to_ist(t_close)
    close_hm = ist_close.strftime("%H:%M")
    in_std_window = in_windows(t_close - 1, S.get("trade_windows", []))
    liquid_start = P.get("liquid_window_start", "09:18")
    liquid_end = P.get("liquid_window_end", "15:05")
    in_liquid_day = liquid_start <= close_hm <= liquid_end

    is_window_ok = in_std_window
    if not in_std_window and in_liquid_day and P.get("allow_midday_with_rvol", True) and ctx:
        crvol = float(ctx.get("crvol", 0.0) or 0.0)
        slot_rvol = float(ctx.get("slot_rvol", 0.0) or 0.0)
        trinity_locked = bool(ctx.get("pcr_trinity_locked", False))
        flow_aligned = bool(ctx.get("writer_flow_aligned", False))
        rvol_break = bool(ctx.get("recent_rvol_break", False))

        # Institutional Activity Override:
        # Never skip midday setups if RVOL in play or Trinity/Writer Flow aligned
        if (crvol >= float(P.get("midday_min_crvol", 1.20)) or
            slot_rvol >= float(P.get("midday_min_slot_rvol", 1.50)) or
            trinity_locked or
            flow_aligned or
            rvol_break):
            is_window_ok = True

    if not is_window_ok:
        reasons.append(f"{close_hm} is outside the trade windows")
    if P.get("require_premium_discount", True) and pd_ok is False:
        reasons.append("entry not in " + ("premium" if side == "short" else "discount"))
    if P.get("require_bias_alignment", True) and bias is not None:
        bt = bias.trend
        if (side == "short" and bt == "bull") or (side == "long" and bt == "bear"):
            reasons.append(f"{bias.tf} trend is {bt}")
    return {"risk": risk, "rr": rr, "target": target, "tsrc": tsrc, "reasons": reasons}


def _nearest_target(an: Analysis, i: int, side: str, entry: float):
    pools = untaken_at(an.pools, i, "SSL" if side == "short" else "BSL")
    if side == "short":
        c = [p for p in pools if p.price < entry]
        p = max(c, key=lambda x: x.price) if c else None
    else:
        c = [p for p in pools if p.price > entry]
        p = min(c, key=lambda x: x.price) if c else None
    return (p.price, p.src) if p else (None, "")


# ── Setup A ──────────────────────────────────────────────────────────────────
def setup_a(an: Analysis, bias: Optional[Analysis], ctx: Optional[Dict[str, Any]] = None) -> List[Signal]:
    P = config.rules()["playbook"]
    b, N = an.bars, len(an.bars)
    out: List[Signal] = []
    # one raid per candle and side: if PDL and a swing low sit at the same price, keep the stronger pool (PDL)
    best: Dict[tuple, Any] = {}
    for x in an.liq:
        if x.kind in ("SWEEP", "FALSE_BREAKOUT"):
            k = (x.i, x.side)
            if k not in best or x.priority > best[k].priority:
                best[k] = x
    raids = sorted(best.values(), key=lambda x: x.i)
    for rd in raids:
        side = "short" if rd.side == "BSL" else "long"
        want = "bear" if side == "short" else "bull"
        story = [f"{hm(rd.ts)} {rd.kind.replace('_', ' ').lower()} of {rd.src} {rd.level:,.1f} (wick {rd.wick:,.1f})"]
        mss = next((e for e in an.events if e.dir == want and rd.i < e.i <= rd.i + int(P["sweep_to_mss_max_bars"])), None)
        cancel = None
        stop_at = (mss.i + int(P["mss_to_entry_max_bars"])) if mss else min(N - 1, rd.i + int(P["sweep_to_mss_max_bars"]))
        for j in range(rd.i + 1, min(N, stop_at + 1)):
            if (side == "short" and b.c[j] > rd.level) or (side == "long" and b.c[j] < rd.level):
                cancel = j
                break
        if cancel is not None and (mss is None or cancel <= mss.i):
            continue                                            # raid failed before anything happened
        if mss is None:
            continue
        story.append(f"{hm(mss.ts)} {'MSS' if mss.is_mss else 'BOS'} {want} through {mss.level:,.1f}"
                     + ("" if mss.displacement else " (no displacement)"))
        if P.get("require_displacement", True) and not mss.displacement:
            continue
        gaps = [g for g in an.fvgs if g.dir == want and rd.i < g.i <= mss.i + 1]
        if not gaps:
            continue
        g = gaps[-1]
        story.append(f"{hm(g.ts)} FVG {g.bottom:,.1f}–{g.top:,.1f} ({g.size:.1f} pts)")
        for k in range(max(mss.i, g.i) + 1, min(N, mss.i + int(P["mss_to_entry_max_bars"]) + 1)):
            if cancel is not None and k >= cancel:
                break
            if side == "short":
                if b.c[k] > g.top:
                    break                                       # gap failed
                hit = b.h[k] >= g.bottom
            else:
                if b.c[k] < g.bottom:
                    break
                hit = b.l[k] <= g.top
            if not hit:
                continue
            entry = float(b.c[k])
            max_risk = float(P.get("max_risk_pts", 45.0))
            sl_buf = float(P.get("sl_buffer_pts", 2.0))
            if side == "short":
                macro_sl = float(b.h[rd.i:k + 1].max()) + sl_buf
                tactical_sl = max(float(b.h[k]), g.top) + sl_buf
                if (macro_sl - entry) > max_risk and (tactical_sl - entry) <= max_risk:
                    sl = tactical_sl
                else:
                    sl = macro_sl
                lo = float(b.l[rd.i:k + 1].min())
                pd_ok = float(b.h[k]) >= (sl + lo) / 2 - sl_buf / 2
            else:
                macro_sl = float(b.l[rd.i:k + 1].min()) - sl_buf
                tactical_sl = min(float(b.l[k]), g.bottom) - sl_buf
                if (entry - macro_sl) > max_risk and (entry - tactical_sl) <= max_risk:
                    sl = tactical_sl
                else:
                    sl = macro_sl
                hi = float(b.h[rd.i:k + 1].max())
                pd_ok = float(b.l[k]) <= (sl + hi) / 2 + sl_buf / 2
            tgt, tsrc = _nearest_target(an, k, side, entry)
            curr_risk = abs(entry - sl)
            if tgt and curr_risk > 0 and abs(tgt - entry) / curr_risk < float(P.get("min_rr", 1.5)):
                if bias is not None and ((side == "long" and bias.trend == "bull") or (side == "short" and bias.trend == "bear")):
                    tgt = (entry + 2.0 * curr_risk) if side == "long" else (entry - 2.0 * curr_risk)
                    tsrc = "2R (trend continuation extension)"
            f = _filters(an, bias, k, side, entry, sl, tgt, tsrc, pd_ok, ctx=ctx)
            story.append(f"{hm(int(b.ts[k]))} retest closed {entry:,.1f} inside/through the gap")
            out.append(Signal(f"A|{an.tf}|{side}|{rd.ts}|{int(b.ts[k])}", "A", "Raid → MSS → FVG", an.tf, side, k,
                              int(b.ts[k]), "SKIP" if f["reasons"] else "TRADE", entry, sl, f["target"], f["risk"],
                              round(f["rr"], 2), f["tsrc"], f["reasons"], story,
                              ctx={"raid_ts": rd.ts, "raid_level": rd.level, "mss_ts": mss.ts, "fvg_ts": g.ts}))
            break
    return out


# ── Setup B ──────────────────────────────────────────────────────────────────
def setup_b(an: Analysis) -> List[Signal]:
    out = []
    for e in an.events:
        if not e.displacement:
            continue
        side = "long" if e.dir == "bull" else "short"
        bar_h = float(an.bars.h[e.i])
        bar_l = float(an.bars.l[e.i])
        bar_range = bar_h - bar_l
        atr = float(an.atr[e.i]) if hasattr(an, "atr") and an.atr is not None and len(an.atr) > e.i else 20.0
        dist_past = (e.level - e.close) if e.dir == "bear" else (e.close - e.level)
        is_extended = (bar_range >= 1.8 * atr or bar_range >= 65.0) and dist_past >= 25.0

        ist_dt = to_ist(e.ts)
        is_eod = (ist_dt.hour == 15 and ist_dt.minute >= 15) or ist_dt.hour > 15
        eod_suffix = " [EOD SETTLEMENT / NEXT-DAY BIAS ONLY — DO NOT OPEN INTRADAY OPTIONS]" if is_eod else ""

        if is_extended:
            if e.dir == "bear":
                retest_lo = min(e.level, e.level + 15)
                retest_hi = max(e.level, e.level + 15)
                sl = max(retest_hi + 15, bar_h)
                nm = f"⚡ DISPLACEMENT BREAKDOWN — Extended {bar_range:.0f}pt bar! DO NOT CHASE @ {e.close:,.1f}; Set Limit Entry on Retest of {retest_lo:,.0f}–{retest_hi:,.0f} (SL {sl:,.0f}){eod_suffix}"
                story = [
                    f"{hm(e.ts)} close {e.close:,.1f} broke {e.level:,.1f} by {dist_past:.1f}pts ({bar_range:.0f}pt bar > 1.8x ATR)",
                    f"Exhaustion guard active: wait for retest of {retest_lo:,.0f}–{retest_hi:,.0f} before entering PE",
                ]
            else:
                retest_lo = min(e.level - 15, e.level)
                retest_hi = max(e.level - 15, e.level)
                sl = min(retest_lo - 15, bar_l)
                nm = f"⚡ DISPLACEMENT BREAKOUT — Extended {bar_range:.0f}pt bar! DO NOT CHASE @ {e.close:,.1f}; Set Limit Entry on Retest of {retest_lo:,.0f}–{retest_hi:,.0f} (SL {sl:,.0f}){eod_suffix}"
                story = [
                    f"{hm(e.ts)} close {e.close:,.1f} broke {e.level:,.1f} by {dist_past:.1f}pts ({bar_range:.0f}pt bar > 1.8x ATR)",
                    f"Exhaustion guard active: wait for retest of {retest_lo:,.0f}–{retest_hi:,.0f} before entering CE",
                ]
            out.append(Signal(f"B|{an.tf}|{e.dir}|{e.ts}", "B", nm, an.tf, side, e.i, e.ts, "INFO", entry=e.level, story=story))
        else:
            nm = f"{'MSS' if e.is_mss else 'BOS'} {'bullish' if e.dir == 'bull' else 'bearish'} through {e.level:,.1f} (displacement){eod_suffix}"
            out.append(Signal(f"B|{an.tf}|{e.dir}|{e.ts}", "B", nm, an.tf, side, e.i, e.ts, "INFO", entry=e.close,
                              story=[f"{hm(e.ts)} close {e.close:,.1f} beyond swing {e.level:,.1f}"]))
    return out


# ── Setup C ──────────────────────────────────────────────────────────────────
def setup_c(an: Analysis, bias: Optional[Analysis], ctx: Optional[Dict[str, Any]] = None) -> List[Signal]:
    P = config.rules()["playbook"]
    r = an.rng
    if not r:
        return []
    b, N = an.bars, len(an.bars)
    lo_b, hi_b = r["ote_band"]
    side = "long" if r["dir"] == "bull" else "short"
    for k in range(r["extreme_i"] + 1, N):
        if side == "long":
            if b.c[k] < r["low"]:
                return []
            touch = b.l[k] <= hi_b and b.c[k] >= lo_b
        else:
            if b.c[k] > r["high"]:
                return []
            touch = b.h[k] >= lo_b and b.c[k] <= hi_b
        if not touch:
            continue
        entry = float(b.c[k])
        max_risk = float(P.get("max_risk_pts", 45.0))
        sl_buf = float(P.get("sl_buffer_pts", 2.0))
        if side == "long":
            macro_sl = r["low"] - sl_buf
            tactical_sl = min(float(b.l[k]), lo_b) - sl_buf
            # If macro swing is too far (>45 pts), anchor tactical SL to local OTE/pullback low
            if (entry - macro_sl) > max_risk and (entry - tactical_sl) <= max_risk:
                sl = tactical_sl
            else:
                sl = macro_sl
            tgt, tsrc = r["high"], "range high (BSL)"
            # Retracement low dipped into discount/OTE
            pd_ok = float(b.l[k]) <= r["eq"]
            # Trend continuation target projection if local high produces tight R:R
            curr_risk = abs(entry - sl)
            if curr_risk > 0 and (tgt - entry) / curr_risk < float(P.get("min_rr", 1.5)):
                ext_tgt = round(r["high"] + 0.618 * (r["high"] - r["low"]), 1)
                if (ext_tgt - entry) / curr_risk >= float(P.get("min_rr", 1.5)):
                    tgt, tsrc = ext_tgt, "1.618x Fib extension"
            if entry > hi_b:
                touch_desc = f"{hm(int(b.ts[k]))} wick tapped OTE/discount support (low {float(b.l[k]):,.1f}) and closed bullishly @ {entry:,.1f}"
            else:
                touch_desc = f"{hm(int(b.ts[k]))} closed {entry:,.1f} inside the OTE band ({r['zone'] if k == N - 1 else 'retrace'})"
        else:
            macro_sl = r["high"] + sl_buf
            tactical_sl = max(float(b.h[k]), hi_b) + sl_buf
            if (macro_sl - entry) > max_risk and (tactical_sl - entry) <= max_risk:
                sl = tactical_sl
            else:
                sl = macro_sl
            tgt, tsrc = r["low"], "range low (SSL)"
            pd_ok = float(b.h[k]) >= r["eq"]
            curr_risk = abs(sl - entry)
            if curr_risk > 0 and (entry - tgt) / curr_risk < float(P.get("min_rr", 1.5)):
                ext_tgt = round(r["low"] - 0.618 * (r["high"] - r["low"]), 1)
                if (entry - ext_tgt) / curr_risk >= float(P.get("min_rr", 1.5)):
                    tgt, tsrc = ext_tgt, "1.618x Fib extension"
            if entry < lo_b:
                touch_desc = f"{hm(int(b.ts[k]))} wick tapped OTE/premium resistance (high {float(b.h[k]):,.1f}) and closed bearishly @ {entry:,.1f}"
            else:
                touch_desc = f"{hm(int(b.ts[k]))} closed {entry:,.1f} inside the OTE band ({r['zone'] if k == N - 1 else 'retrace'})"

        f = _filters(an, bias, k, side, entry, sl, tgt, tsrc, pd_ok, ctx=ctx)
        story = [f"{hm(r['event_ts'])} {r['event']} {r['dir']} → range {r['low']:,.1f}–{r['high']:,.1f}, EQ {r['eq']:,.1f}",
                 f"OTE 61.8–79 %: {lo_b:,.1f}–{hi_b:,.1f}",
                 touch_desc]
        return [Signal(f"C|{an.tf}|{side}|{r['event_ts']}", "C", "OTE retracement", an.tf, side, k, int(b.ts[k]),
                       "SKIP" if f["reasons"] else "TRADE", entry, sl, f["target"], f["risk"], round(f["rr"], 2),
                       f["tsrc"], f["reasons"], story)]
    return []


# ── Setup Deep Reversal ───────────────────────────────────────────────────────
def setup_deep_reversal(an: Analysis, bias: Optional[Analysis]) -> List[Signal]:
    """
    🚨 A+ DEEP-LEVEL LIQUIDITY SWEEP & V-REVERSAL ENGINE
    Detects 3-factor extreme session reversal:
      1. Deep Liquidity Sweep: >= 100 pt range, session low/high swept or Higher Low within 30 pts.
      2. Rejection tail / absorption at extreme.
      3. Micro-structure MSS break in the reversal direction.
    """
    b = an.bars
    N = len(b)
    if N < 12:
        return []

    sess_low = float(np.min(b.l))
    sess_high = float(np.max(b.h))
    sess_range = sess_high - sess_low
    if sess_range < 100:
        return []

    sess_low_i = int(np.argmin(b.l))
    sess_high_i = int(np.argmax(b.h))

    out = []
    for e in an.events:
        if not e.is_mss:
            continue
        k = e.i
        ist_dt = to_ist(e.ts)
        is_eod = (ist_dt.hour == 15 and ist_dt.minute >= 15) or ist_dt.hour > 15
        eod_suffix = " [EOD SETTLEMENT / NEXT-DAY BIAS ONLY — DO NOT OPEN INTRADAY OPTIONS]" if is_eod else ""

        if e.dir == "bull":
            bars_since_low = k - sess_low_i
            if 0 < bars_since_low <= 15:
                sweep_bar_l = float(b.l[sess_low_i])
                sweep_bar_h = float(b.h[sess_low_i])
                sweep_bar_c = float(b.c[sess_low_i])
                rng_sweep = max(1.0, sweep_bar_h - sweep_bar_l)
                has_rejection = sweep_bar_c >= sweep_bar_l + 0.3 * rng_sweep
                is_higher_low = sess_low <= float(b.l[k]) <= sess_low + 30

                if has_rejection or is_higher_low:
                    entry = float(e.close)
                    sl = round(sess_low - 12.0, 1)
                    tp = round(sess_low + sess_range * 0.5, 1)
                    risk = round(entry - sl, 1)
                    if risk > 0:
                        rr = round((tp - entry) / risk, 2)
                        strike = int(round(entry / 50.0) * 50)
                        nm = f"🚨 A+ DEEP REVERSAL SNIPER: BUY {strike} CE{eod_suffix}"
                        story = [
                            f"Deep session low swept @ {sess_low:,.1f} ({sess_range:,.0f}pt drop) with {'rejection tail' if has_rejection else 'Higher Low'}",
                            f"MSS bullish break through {e.level:,.1f} confirmed @ {entry:,.1f}",
                            f"Entry {entry:,.1f} · SL {sl:,.1f} (below sweep low) · Tgt {tp:,.1f} (50% Fib recovery, RR {rr})",
                        ]
                        out.append(Signal(
                            f"DEEP_REV|{an.tf}|bull|{e.ts}",
                            "DEEP_REV",
                            nm,
                            an.tf,
                            "long",
                            k,
                            e.ts,
                            "TRADE",
                            entry=entry,
                            sl=sl,
                            tp=tp,
                            risk=risk,
                            rr=rr,
                            target_src="50% Fib / VWAP",
                            reasons=[],
                            story=story,
                        ))

        elif e.dir == "bear":
            bars_since_high = k - sess_high_i
            if 0 < bars_since_high <= 15:
                sweep_bar_l = float(b.l[sess_high_i])
                sweep_bar_h = float(b.h[sess_high_i])
                sweep_bar_c = float(b.c[sess_high_i])
                rng_sweep = max(1.0, sweep_bar_h - sweep_bar_l)
                has_rejection = sweep_bar_c <= sweep_bar_h - 0.3 * rng_sweep
                is_lower_high = sess_high - 30 <= float(b.h[k]) <= sess_high

                if has_rejection or is_lower_high:
                    entry = float(e.close)
                    sl = round(sess_high + 12.0, 1)
                    tp = round(sess_high - sess_range * 0.5, 1)
                    risk = round(sl - entry, 1)
                    if risk > 0:
                        rr = round((entry - tp) / risk, 2)
                        strike = int(round(entry / 50.0) * 50)
                        nm = f"🚨 A+ DEEP REVERSAL SNIPER: BUY {strike} PE{eod_suffix}"
                        story = [
                            f"Deep session high swept @ {sess_high:,.1f} ({sess_range:,.0f}pt rally) with {'rejection tail' if has_rejection else 'Lower High'}",
                            f"MSS bearish break through {e.level:,.1f} confirmed @ {entry:,.1f}",
                            f"Entry {entry:,.1f} · SL {sl:,.1f} (above sweep high) · Tgt {tp:,.1f} (50% Fib recovery, RR {rr})",
                        ]
                        out.append(Signal(
                            f"DEEP_REV|{an.tf}|bear|{e.ts}",
                            "DEEP_REV",
                            nm,
                            an.tf,
                            "short",
                            k,
                            e.ts,
                            "TRADE",
                            entry=entry,
                            sl=sl,
                            tp=tp,
                            risk=risk,
                            rr=rr,
                            target_src="50% Fib / VWAP",
                            reasons=[],
                            story=story,
                        ))

    return out


def setup_early_reversal(an: Analysis, bias: Optional[Analysis], ctx: Optional[Dict[str, Any]] = None) -> List[Signal]:
    """
    🎯 A+ EARLY SESSION REVERSAL SNIPER (09:18–09:50 IST)
    Catches the early morning bottom/top reversal before giant 94pt+ bars:
      1. Opening drive sweeps key level / session extreme (09:15–09:35) on exhausted follow-through
         or prints a rejection wick / Higher Low (for Bull) or Lower High (for Bear).
      2. 3m/5m micro-structure breaks the immediate internal high with candle body range < 55 pts.
      3. Produces tight risk (20–35 pts) before the 90pt+ runaway expansion happens.
    """
    b = an.bars
    N = len(b)
    if N < 3:
        return []

    out = []
    for k in range(1, min(N, 12)):
        ist_dt = to_ist(b.ts[k])
        hhmm = ist_dt.strftime("%H:%M")
        if not ("09:18" <= hhmm <= "09:50"):
            continue

        bar_h = float(b.h[k])
        bar_l = float(b.l[k])
        bar_c = float(b.c[k])
        bar_o = float(b.o[k])
        body = abs(bar_c - bar_o)
        rng = bar_h - bar_l
        if body > 55.0:
            continue

        prior_low = float(np.min(b.l[:k]))
        prior_high = float(np.max(b.h[:k]))

        # Bullish Early Reversal
        is_hl = prior_low <= bar_l <= prior_low + 30.0
        has_bull_rejection = (bar_c >= bar_l + 0.35 * max(rng, 1.0)) and bar_c >= bar_o
        breaks_micro_high = bar_c > float(b.h[k - 1])

        if (is_hl or bar_l <= prior_low) and has_bull_rejection and breaks_micro_high:
            sl = round(prior_low - 8.0, 1)
            risk = round(bar_c - sl, 1)
            if 15.0 <= risk <= 40.0:
                tp = round(bar_c + 2.5 * risk, 1)
                rr = round((tp - bar_c) / risk, 2)
                strike = int(round(bar_c / 50.0) * 50)
                nm = f"🎯 A+ EARLY REVERSAL SNIPER: BUY {strike} CE @ {bar_c:,.1f}"
                story = [
                    f"{hhmm} opening sweep/HL @ {prior_low:,.1f} held; micro-high broke @ {bar_c:,.1f} (tight {risk:.1f}pt risk)",
                    f"Option flow confirmed bullish support floor · SL {sl:,.1f} · Tgt {tp:,.1f} (RR {rr})",
                ]
                out.append(Signal(
                    f"EARLY_REV|{an.tf}|bull|{int(b.ts[k])}",
                    "EARLY_REV",
                    nm,
                    an.tf,
                    "long",
                    k,
                    int(b.ts[k]),
                    "TRADE",
                    entry=bar_c,
                    sl=sl,
                    tp=tp,
                    risk=risk,
                    rr=rr,
                    target_src="Morning Recovery / 2.5R",
                    reasons=[],
                    story=story,
                ))

        # Bearish Early Reversal
        is_lh = prior_high - 30.0 <= bar_h <= prior_high
        has_bear_rejection = (bar_c <= bar_h - 0.35 * max(rng, 1.0)) and bar_c <= bar_o
        breaks_micro_low = bar_c < float(b.l[k - 1])

        if (is_lh or bar_h >= prior_high) and has_bear_rejection and breaks_micro_low:
            sl = round(prior_high + 8.0, 1)
            risk = round(sl - bar_c, 1)
            if 15.0 <= risk <= 40.0:
                tp = round(bar_c - 2.5 * risk, 1)
                rr = round((bar_c - tp) / risk, 2)
                strike = int(round(bar_c / 50.0) * 50)
                nm = f"🎯 A+ EARLY REVERSAL SNIPER: BUY {strike} PE @ {bar_c:,.1f}"
                story = [
                    f"{hhmm} opening high/LH @ {prior_high:,.1f} held; micro-low broke @ {bar_c:,.1f} (tight {risk:.1f}pt risk)",
                    f"Option flow confirmed bearish ceiling · SL {sl:,.1f} · Tgt {tp:,.1f} (RR {rr})",
                ]
                out.append(Signal(
                    f"EARLY_REV|{an.tf}|bear|{int(b.ts[k])}",
                    "EARLY_REV",
                    nm,
                    an.tf,
                    "short",
                    k,
                    int(b.ts[k]),
                    "TRADE",
                    entry=bar_c,
                    sl=sl,
                    tp=tp,
                    risk=risk,
                    rr=rr,
                    target_src="Morning Reversal / 2.5R",
                    reasons=[],
                    story=story,
                ))
    return out


def evaluate(primary: Analysis, bias: Optional[Analysis], ctx: Optional[Dict[str, Any]] = None) -> List[Signal]:
    sigs = (setup_a(primary, bias, ctx=ctx) +
            setup_b(primary) +
            setup_c(primary, bias, ctx=ctx) +
            setup_deep_reversal(primary, bias) +
            setup_early_reversal(primary, bias, ctx=ctx))
    sigs.sort(key=lambda s: (s.i, s.setup))
    return sigs


def live(sigs: List[Signal], an: Analysis) -> List[Signal]:
    """Signals whose trigger candle is the candle that just closed — the only ones that may alert / trade."""
    return [s for s in sigs if s.i == an.last_i]
