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
             target: Optional[float], tsrc: str, pd_ok: Optional[bool]) -> Dict[str, Any]:
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
    if not in_windows(t_close - 1, S["trade_windows"]):
        reasons.append(f"{to_ist(t_close).strftime('%H:%M')} is outside the trade windows")
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
def setup_a(an: Analysis, bias: Optional[Analysis]) -> List[Signal]:
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
            if side == "short":
                sl = float(b.h[rd.i:k + 1].max()) + float(P["sl_buffer_pts"])
                lo = float(b.l[rd.i:k + 1].min())
                pd_ok = entry >= (sl + lo) / 2 - float(P["sl_buffer_pts"]) / 2
            else:
                sl = float(b.l[rd.i:k + 1].min()) - float(P["sl_buffer_pts"])
                hi = float(b.h[rd.i:k + 1].max())
                pd_ok = entry <= (sl + hi) / 2 + float(P["sl_buffer_pts"]) / 2
            tgt, tsrc = _nearest_target(an, k, side, entry)
            f = _filters(an, bias, k, side, entry, sl, tgt, tsrc, pd_ok)
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
        nm = f"{'MSS' if e.is_mss else 'BOS'} {'bullish' if e.dir == 'bull' else 'bearish'} through {e.level:,.1f} (displacement)"
        out.append(Signal(f"B|{an.tf}|{e.dir}|{e.ts}", "B", nm, an.tf, side, e.i, e.ts, "INFO", entry=e.close,
                          story=[f"{hm(e.ts)} close {e.close:,.1f} beyond swing {e.level:,.1f}"]))
    return out


# ── Setup C ──────────────────────────────────────────────────────────────────
def setup_c(an: Analysis, bias: Optional[Analysis]) -> List[Signal]:
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
        if side == "long":
            sl, tgt, tsrc = r["low"] - float(P["sl_buffer_pts"]), r["high"], "range high (BSL)"
            pd_ok = entry <= r["eq"]
        else:
            sl, tgt, tsrc = r["high"] + float(P["sl_buffer_pts"]), r["low"], "range low (SSL)"
            pd_ok = entry >= r["eq"]
        f = _filters(an, bias, k, side, entry, sl, tgt, tsrc, pd_ok)
        story = [f"{hm(r['event_ts'])} {r['event']} {r['dir']} → range {r['low']:,.1f}–{r['high']:,.1f}, EQ {r['eq']:,.1f}",
                 f"OTE 61.8–79 %: {lo_b:,.1f}–{hi_b:,.1f}",
                 f"{hm(int(b.ts[k]))} closed {entry:,.1f} inside the OTE band ({r['zone'] if k == N - 1 else 'retrace'})"]
        return [Signal(f"C|{an.tf}|{side}|{r['event_ts']}", "C", "OTE retracement", an.tf, side, k, int(b.ts[k]),
                       "SKIP" if f["reasons"] else "TRADE", entry, sl, f["target"], f["risk"], round(f["rr"], 2),
                       f["tsrc"], f["reasons"], story)]
    return []


def evaluate(primary: Analysis, bias: Optional[Analysis]) -> List[Signal]:
    sigs = setup_a(primary, bias) + setup_b(primary) + setup_c(primary, bias)
    sigs.sort(key=lambda s: (s.i, s.setup))
    return sigs


def live(sigs: List[Signal], an: Analysis) -> List[Signal]:
    """Signals whose trigger candle is the candle that just closed — the only ones that may alert / trade."""
    return [s for s in sigs if s.i == an.last_i]
