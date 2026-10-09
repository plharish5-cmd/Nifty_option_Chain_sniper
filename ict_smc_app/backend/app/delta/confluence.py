"""
delta/confluence.py — raises or lowers the CONFIDENCE of Setup A / C. It never creates a trade, never removes
one, never changes TRADE ↔ SKIP, entry, stop or target. The triggers stay exactly the playbook's.

Only events that are KNOWN at the entry candle's close count (event ts ≤ entry ts), from the last
`lookback_bars` candles. Each adjustment carries a plain-English line, shown on the signal card.

    short + DELTA_ABSORPTION (bear) at a SUPPLY order block       +ob_absorption_bonus
    short + DELTA_ABSORPTION (bear) at a SUPPLY FVG               +fvg_absorption_bonus
    A + sweep, then DELTA_FLIP / ABSORPTION in the trade direction
        within flip.max_bars of the raid                         +sweep_flip_bonus
    FOOTPRINT_IMBALANCE in the trade direction at a zone          +imbalance_bonus
    DELTA_AGGRESSION against the trade, not absorbed (no absorption event on that candle)
                                                                  −against_aggression_penalty
    (mirror for longs: DEMAND zones, bull events)

Breakdown (shown as three bars on the card; confidence = their sum, clamped 0…1):
    structure        confluence.base (the playbook already passed every structure filter)
    options context  ± confluence.options_context.* — selected strikes' build/unwind vs the setup side.
                     Scored ONLY for a signal on the candle that just closed (the chain is "now"; older signals
                     show "not recorded").
    flow             delta / footprint adjustments above (strike flow included, mapped to NIFTY direction),
                     or — only when delta is unavailable and the user enabled it — Options Flow Proxy:
                     PROXY_FLOW_SURGE / FADE in the trade direction +proxy_surge_bonus, against −proxy_against_penalty.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .. import config
from ..timeutil import TF_SECONDS, hm


def _parts(s, base: float, ctx: Optional[float], flow: float, src: str) -> None:
    s.confidence_parts = {"structure": round(base, 2), "options_context": None if ctx is None else round(ctx, 2),
                          "flow": round(flow, 2), "flow_source": src}
    s.confidence = round(min(1.0, max(0.0, base + (ctx or 0.0) + flow)), 2)


def _context(s, C, context: Optional[Dict[str, Any]], live_i: Optional[int]) -> Optional[float]:
    if not context or live_i is None or s.i != live_i:
        if context is not None:
            s.confluence.append("Options context: not recorded for this (older) candle — chain is 'now' only.")
        return None
    from ..strikes.context import verdict
    v = verdict(s.side, context.get("foci", []))
    w = C.get("options_context", {})
    adj = {"supportive": float(w.get("supportive", 0.05)), "opposing": float(w.get("opposing", -0.05))}.get(v["verdict"], 0.0)
    s.confluence.append(f"{'+' if adj >= 0 else '−'}{abs(adj):.2f} · options context: {v['lines'][0]}")
    s.confluence += [f"   {x}" for x in v["lines"][1:3]]
    return adj


def _proxy(s, C, proxy_res: Dict[str, Any], step: int) -> float:
    from .proxy import LABEL
    want = "bear" if s.side == "short" else "bull"
    t0 = s.ts - int(C["lookback_bars"]) * step
    recent = [e for e in proxy_res.get("events", []) if t0 <= e["ts"] <= s.ts]
    adj = 0.0
    w = next((e for e in reversed(recent) if e["direction"] == want), None)
    a = next((e for e in reversed(recent) if e["direction"] != want and e["event_type"] == "PROXY_FLOW_SURGE"), None)
    if w:
        adj += float(C.get("proxy_surge_bonus", 0.05))
        s.confluence.append(f"+{float(C.get('proxy_surge_bonus', 0.05)):.2f} · {LABEL}: {w['time']} "
                            f"{w['event_type'].replace('PROXY_FLOW_', '').lower()} in the trade direction.")
    if a:
        adj -= float(C.get("proxy_against_penalty", 0.05))
        s.confluence.append(f"−{float(C.get('proxy_against_penalty', 0.05)):.2f} · {LABEL}: {a['time']} surge against the trade.")
    if not w and not a:
        s.confluence.append(f"{LABEL}: nothing near the entry.")
    return adj


def apply(signals: List[Any], delta_res: Optional[Dict[str, Any]], proxy_res: Optional[Dict[str, Any]] = None,
          context: Optional[Dict[str, Any]] = None, live_i: Optional[int] = None) -> List[Any]:
    """delta_res: delta/engine.compute() output — index delta and/or strike flow (events already in NIFTY direction).
    proxy_res: delta/proxy.compute() output (used only when delta is unavailable).
    context:  {"foci": [strikes.context.focus(...)]} for the live candle; live_i: index of that candle."""
    C = config.rules()["confluence"]
    base = float(C["base"])
    for s in signals:
        if s.setup not in ("A", "C"):
            continue
        s.confidence, s.confluence = base, []
        ctx = _context(s, C, context, live_i)
        if not delta_res or not delta_res.get("available"):
            if C.get("delta_unavailable_note", True):
                s.confluence.insert(0, "Delta unavailable — confidence from structure only.")
            if proxy_res and proxy_res.get("available"):
                _parts(s, base, ctx, _proxy(s, C, proxy_res, TF_SECONDS[s.tf]), "proxy")
            else:
                _parts(s, base, ctx, 0.0, "none")
            continue
        if delta_res.get("mock"):
            s.confluence.append("MOCK delta — these adjustments are for testing the UI only.")
        step = TF_SECONDS[s.tf]
        t0 = s.ts - int(C["lookback_bars"]) * step
        want = "bear" if s.side == "short" else "bull"
        zside = "SUPPLY" if s.side == "short" else "DEMAND"
        evs = [e for e in delta_res.get("events", []) if e["ts"] <= s.ts]
        recent = [e for e in evs if e["ts"] >= t0]
        adj: List[tuple] = []

        ob_abs = next((e for e in recent if e["event_type"] == "DELTA_ABSORPTION" and e["direction"] == want
                       and e.get("zone_type") == "ORDER_BLOCK" and e.get("zone_side") == zside), None)
        if ob_abs:
            who = "Buyers" if s.side == "short" else "Sellers"
            adj.append((float(C["ob_absorption_bonus"]),
                        f"{ob_abs['time']} {who} attacked inside {ob_abs['zone']} but price didn't move "
                        f"{'up' if s.side == 'short' else 'down'} → absorption → {s.side} bias stronger."))
        fvg_abs = next((e for e in recent if e["event_type"] == "DELTA_ABSORPTION" and e["direction"] == want
                        and e.get("zone_type") == "FVG" and e.get("zone_side") == zside), None)
        if fvg_abs:
            adj.append((float(C["fvg_absorption_bonus"]),
                        f"{fvg_abs['time']} aggression absorbed inside {fvg_abs['zone']} → the gap is being defended."))

        raid_ts = (getattr(s, "ctx", None) or {}).get("raid_ts")
        if s.setup == "A" and raid_ts is not None:
            r1 = raid_ts + int(config.rules()["delta"]["flip"]["max_bars"]) * step
            fl = next((e for e in evs if e["event_type"] in ("DELTA_FLIP", "DELTA_ABSORPTION") and e["direction"] == want
                       and raid_ts <= e["ts"] <= r1), None)
            if fl:
                adj.append((float(C["sweep_flip_bonus"]),
                            f"{hm(raid_ts)} sweep, then {fl['time']} delta "
                            f"{'flipped to ' + ('sellers' if want == 'bear' else 'buyers') if fl['event_type'] == 'DELTA_FLIP' else 'was absorbed'}"
                            f" at the sweep → reversal confidence up."))
        imb = next((e for e in recent if e["event_type"] == "FOOTPRINT_IMBALANCE" and e["direction"] == want
                    and e.get("zone_id")), None)
        if imb:
            adj.append((float(C["imbalance_bonus"]),
                        f"{imb['time']} {'proxy ' if imb.get('proxy') else ''}imbalance in the trade direction at {imb['zone']}."))
        abs_ts = {e["ts"] for e in recent if e["event_type"] == "DELTA_ABSORPTION"}
        against = next((e for e in reversed(recent) if e["event_type"] == "DELTA_AGGRESSION" and e["direction"] != want
                        and e["ts"] not in abs_ts), None)
        if against:
            who = "Buyers" if against["direction"] == "bull" else "Sellers"
            adj.append((-float(C["against_aggression_penalty"]),
                        f"{against['time']} {who} attacked (delta {against['delta']:+,.0f}) and were NOT absorbed → "
                        f"{s.side} confidence lower."))
        _parts(s, base, ctx, sum(a for a, _ in adj), "delta")
        s.confluence += [f"{'+' if a >= 0 else '−'}{abs(a):.2f} · {t}" for a, t in adj]
        if not adj:
            s.confluence.append("No delta confluence near the entry — confidence from structure only.")
    return signals
