"""
engine/analyze.py — runs every concept on CLOSED candles of one timeframe and returns
  * an Analysis object (indices, for the playbook), and
  * to_json(): what the chart draws (zones, markers, lines) with chart-ready IST times.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np

from .. import config
from ..timeutil import chart_time, hm
from .fvg import FVG, find_fvgs
from .liquidity import LiqEvent, Pool, build_pools, scan, untaken_at
from .orderblocks import OrderBlock, find_order_blocks
from .primitives import Bars, Swing, atr, pivots, zigzag
from .ranges import dealing_range, external_pools, session_levels
from .structure import StructEvent, structure


@dataclass
class Analysis:
    tf: str
    bars: Bars
    atr: np.ndarray
    swings: List[Swing]
    zz: List[Swing]
    events: List[StructEvent]
    trend: str
    fvgs: List[FVG]
    obs: List[OrderBlock]
    pools: List[Pool]
    liq: List[LiqEvent]
    rng: Optional[Dict[str, Any]]
    sess: Dict[str, Any] = field(default_factory=dict)

    @property
    def last_i(self) -> int:
        return len(self.bars) - 1


def analyze(rows: List[Dict[str, float]], tf: str, rows_1m: Optional[List[Dict[str, float]]] = None,
            daily: Optional[List[Dict[str, Any]]] = None) -> Optional[Analysis]:
    """rows: CLOSED candles of tf (oldest first). rows_1m: closed 1m candles for session levels."""
    E = config.rules()["engine"]
    rows = [r for r in rows if r.get("closed", True)][-int(E["lookback_bars"]):]
    if len(rows) < 10:
        return None
    b = Bars.from_rows(rows)
    a = atr(b, int(E["atr_len"]))
    n = int(E["pivot_n"].get(tf, 2))
    sw = pivots(b, n)
    fv = find_fvgs(b, a, float(E["fvg_min_pts"]), float(E["fvg_min_atr"]))
    fvg_bars = {g.i: g.dir for g in fv}
    ev, trend = structure(b, sw, a, fvg_bars, float(E["displacement_body_atr"]), float(E["displacement_body_ratio"]))
    obs = find_order_blocks(b, ev, int(E["ob_max_age_bars"]))
    sess = session_levels(rows_1m or [], daily or []) if rows_1m else {}
    pools = build_pools(b, sw, a, float(E["eq_tolerance_pts"]), float(E["eq_tolerance_atr"]), external_pools(sess))
    liq = scan(b, pools, float(E["sweep_min_pen_pts"]), int(E["false_breakout_max_bars"]))
    rng = dealing_range(b, ev, [float(x) for x in E["ote_levels"]])
    return Analysis(tf, b, a, sw, zigzag(sw), ev, trend, fv, obs, pools, liq, rng, sess)


def to_json(an: Analysis, max_items: int = 40, near_atr: float = 4.0, max_zones: int = 4,
            marker_bars: int = 150) -> Dict[str, Any]:
    """Only what a trader needs on screen: zones within near_atr × ATR of price (newest max_zones of each kind),
    liquidity within the same band (session pools always), markers from the last marker_bars bars."""
    b, N = an.bars, len(an.bars)
    end_t = chart_time(int(b.ts[-1]))
    px = float(b.c[-1])
    A = float(an.atr[-1])
    lo_band, hi_band = px - near_atr * A, px + near_atr * A
    near = lambda top, bot: bot <= hi_band and top >= lo_band          # noqa: E731
    T = lambda i: chart_time(int(b.ts[min(max(i, 0), N - 1)]))          # noqa: E731

    zones = []
    fv = [g for g in an.fvgs if g.state_at(N - 1) not in ("invalid", "filled") and near(g.top, g.bottom)]
    for g in fv[-max_zones:]:
        zones.append({"type": "FVG", "dir": g.dir, "from": T(g.i - 2), "to": end_t, "top": g.top, "bottom": g.bottom,
                      "state": g.state_at(N - 1), "label": f"{'Bull' if g.dir == 'bull' else 'Bear'} FVG {g.size:.1f}",
                      "formed": hm(g.ts)})
    ob = [o for o in an.obs if o.state not in ("broken", "expired") and near(o.top, o.bottom)]
    for o in ob[-max_zones:]:
        zones.append({"type": o.kind, "dir": o.dir, "from": T(o.i), "to": end_t, "top": o.top, "bottom": o.bottom,
                      "state": o.state,
                      "label": f"{'Demand' if o.dir == 'bull' else 'Supply'} {o.kind} · {o.state}",
                      "formed": hm(o.ts)})

    pools = []
    for p in untaken_at(an.pools, N - 1, "BSL") + untaken_at(an.pools, N - 1, "SSL"):
        if p.src in ("SWING", "EQH", "EQL") and not (lo_band <= p.price <= hi_band):
            continue
        pools.append({"side": p.side, "src": p.src, "price": p.price, "from": T(p.known_i), "members": p.members})
    pools.sort(key=lambda p: abs(p["price"] - px))
    pools = sorted(pools[:8], key=lambda p: -p["price"])

    first = max(0, N - marker_bars)
    markers = []
    for e in an.events:
        if e.i < first:
            continue
        markers.append({"time": T(e.i), "kind": "MSS" if e.is_mss else "BOS", "dir": e.dir, "price": e.level,
                        "text": f"{'MSS' if e.is_mss else 'BOS'}{'' if e.displacement else '·weak'}",
                        "at": hm(e.ts)})
    for x in an.liq:
        if x.kind == "RUN" or x.i < first:
            continue
        markers.append({"time": T(x.i), "kind": x.kind, "dir": x.dir, "price": x.wick,
                        "text": ("Sweep " if x.kind == "SWEEP" else "False BO ") + (x.src if x.src != "SWING" else ""),
                        "at": hm(x.ts)})
    markers.sort(key=lambda m: m["time"])
    markers = markers[-max_items:]

    zz = [{"time": T(s.i), "price": s.price, "label": s.label} for s in an.zz[-60:]]
    rng = None
    if an.rng:
        r = an.rng
        rng = {k: r[k] for k in ("dir", "event", "high", "low", "eq", "ote", "ote_band", "pos_pct", "zone")}
        rng["from"] = T(r["origin_i"])
        rng["to"] = end_t
    return {"tf": an.tf, "trend": an.trend, "atr": round(A, 2), "last_close": px,
            "last_ts": int(b.ts[-1]), "zones": zones, "pools": pools, "markers": markers, "zigzag": zz,
            "range": rng, "session": an.sess}
