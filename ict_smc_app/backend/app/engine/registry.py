"""
engine/registry.py — ONE record shape for every zone the engine knows: liquidity pools, order blocks, FVGs.

It does not detect anything itself. It reads the existing detectors (liquidity.py, orderblocks.py, fvg.py) and
normalizes their output, so the guide's semantics stay exactly as they are:

  LIQ_POOL      BSL / SSL      fresh → swept (wick beyond, close back inside) | run (close beyond)
  ORDER_BLOCK   SUPPLY/DEMAND  fresh → mitigated → respected | broken (→ a separate BREAKER record)
                               breaker → breaker_mitigated → breaker_respected | breaker_broken ; expired
  FVG           SUPPLY/DEMAND  fresh → touched → ce (50 %) → filled | invalid (close through the far edge)
  any           void           corrected candles no longer produce this zone (see reconcile)

Times are epoch seconds of the START of the closed candle that caused the change (the same convention as every
"at 14:35" label in the app). A zone created by the candle starting 14:30 is known from 14:35, so it can only
be "hit" by candles that start AFTER created_at.

PSEUDOCODE — reconcile(tf, fresh_zones, window, after_repair)
    stored = zones in SQLite for tf
    for z in fresh_zones:                       # recomputed from CLOSED candles
        s = stored.get(z.id)                    # id = type|tf|side|created_at|low|high  (timestamps, not indices)
        if s is None:   insert z                # first sighting
        else:           s = merge_forward(s, z) # set-once timestamps, state rank only increases
    if after_repair and void_missing_after_repair:
        for s in stored not in fresh_zones:
            if window.start + guard ≤ s.created_at ≤ window.end and s.state != void:
                s.state = void                  # e.g. a fake FVG from a feed stall, fixed by the REST download
    → never deletes, never duplicates, never moves a timestamp backwards.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from .. import config, db

RANK = {
    "LIQ_POOL": {"fresh": 0, "swept": 1, "run": 2},
    "ORDER_BLOCK": {"fresh": 0, "mitigated": 1, "respected": 2, "broken": 3, "expired": 3,
                    "breaker": 0, "breaker_mitigated": 1, "breaker_respected": 2, "breaker_broken": 3},
    "FVG": {"fresh": 0, "touched": 1, "ce": 2, "filled": 3, "invalid": 4},
}
TERMINAL = {"run", "broken", "expired", "breaker_broken", "filled", "invalid", "void"}
VOID_RANK = 99
_FVG_STATE = {"open": "fresh", "touched": "touched", "ce": "ce", "filled": "filled", "invalid": "invalid"}


@dataclass
class Zone:
    id: str
    type: str                       # LIQ_POOL | ORDER_BLOCK | FVG
    timeframe: str
    side: str                       # BSL | SSL | SUPPLY | DEMAND
    price_low: float
    price_high: float
    created_at: int
    state: str
    mitigated_at: Optional[int] = None
    invalidated_at: Optional[int] = None
    source_event_id: Optional[str] = None
    label: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def mid(self) -> float:
        return (self.price_low + self.price_high) / 2

    @property
    def is_level(self) -> bool:
        return self.price_low == self.price_high

    def active_for(self, bar_ts: int) -> bool:
        """Could a candle starting at bar_ts interact with this zone? Known only after its creating candle
        closed; still 'at the zone' on the candle that invalidates it."""
        if self.state == "void" or bar_ts <= self.created_at:
            return False
        ends = [self.invalidated_at]
        if self.type == "LIQ_POOL":
            ends.append(self.mitigated_at)           # a pool is consumed by its sweep (or run)
        elif self.type == "FVG":
            ends.append(self.extra.get("filled_at"))  # a filled gap is efficient again
        ends = [e for e in ends if e is not None]
        return not ends or bar_ts <= min(ends)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _rp(x: float) -> float:
    step = float(config.rule("zones", "price_round", 0.05)) or 0.05
    return round(round(float(x) / step) * step, 2)


def zone_id(type_: str, tf: str, side: str, created_at: int, low: float, high: float) -> str:
    raw = f"{type_}|{tf}|{side}|{int(created_at)}|{_rp(low):.2f}|{_rp(high):.2f}"
    return f"{type_[:3]}-{tf}-{int(created_at)}-" + hashlib.sha1(raw.encode()).hexdigest()[:8]


def struct_event_id(tf: str, ts: int, d: str) -> str:
    return f"STRUCT|{tf}|{d}|{int(ts)}"


def liq_event_id(tf: str, kind: str, side: str, ts: int, level: float) -> str:
    return f"LIQ|{tf}|{kind}|{side}|{int(ts)}|{_rp(level):.2f}"


# ── build: engine output → zone records (pure, no I/O) ────────────────────────
def build(an) -> List[Zone]:
    b, tf = an.bars, an.tf
    N = len(b)
    T = lambda i: int(b.ts[i]) if i is not None and 0 <= i < N else None      # noqa: E731
    out: List[Zone] = []
    ev_at = {e.i: e for e in an.events}

    for p in an.pools:
        st = "fresh"
        mit = inv = None
        if p.taken_i is not None:
            if p.taken_how == "sweep":
                st, mit = "swept", T(p.taken_i)
            else:
                st, inv = "run", T(p.taken_i)
        created = T(p.known_i)
        if created is None:
            continue
        src_ev = None
        if p.taken_i is not None:
            src_ev = liq_event_id(tf, "SWEEP" if p.taken_how == "sweep" else "RUN", p.side, T(p.taken_i), p.price)
        extra = {"src": p.src, "members": p.members}
        if p.fb_i is not None:
            extra["false_breakout_at"] = T(p.fb_i)
        out.append(Zone(zone_id("LIQ_POOL", tf, p.side, created, p.price, p.price), "LIQ_POOL", tf, p.side,
                        float(p.price), float(p.price), created, st, mit, inv, src_ev,
                        f"{p.side} {p.src} {p.price:,.1f}", extra))

    for o in an.obs:
        side = "DEMAND" if o.dir == "bull" else "SUPPLY"
        created = T(o.created_i)
        if created is None:
            continue
        st = o.state
        if o.kind == "BREAKER":
            st = "breaker" if st == "fresh" else ("expired" if st == "expired" else f"breaker_{st}")
        ev = ev_at.get(o.created_i)
        extra = {"kind": o.kind, "anchor_at": T(o.i), "mean": round(o.mean, 2)}
        if o.respected_i is not None:
            extra["respected_at"] = T(o.respected_i)
        out.append(Zone(zone_id("ORDER_BLOCK", tf, side, created, o.bottom, o.top), "ORDER_BLOCK", tf, side,
                        float(o.bottom), float(o.top), created, st, T(o.mitigated_i), T(o.broken_i),
                        struct_event_id(tf, ev.ts, ev.dir) if ev and o.kind == "OB" else None,
                        f"{side.title()} {'breaker' if o.kind == 'BREAKER' else 'OB'} {o.bottom:,.1f}–{o.top:,.1f}",
                        extra))

    for g in an.fvgs:
        side = "DEMAND" if g.dir == "bull" else "SUPPLY"
        created = T(g.i)
        ev = ev_at.get(g.i) or ev_at.get(g.i + 1)
        st = _FVG_STATE[g.state_at(N - 1)]
        extra = {"anchor_at": T(g.i - 2), "size": round(g.size, 2), "ce": round(g.ce, 2)}
        if g.ce_i is not None:
            extra["ce_at"] = T(g.ce_i)
        if g.filled_i is not None:
            extra["filled_at"] = T(g.filled_i)
        out.append(Zone(zone_id("FVG", tf, side, created, g.bottom, g.top), "FVG", tf, side,
                        float(g.bottom), float(g.top), created, st, T(g.touched_i), T(g.invalid_i),
                        struct_event_id(tf, ev.ts, ev.dir) if ev and ev.dir == g.dir else None,
                        f"{'Bull' if g.dir == 'bull' else 'Bear'} FVG {g.bottom:,.1f}–{g.top:,.1f}", extra))
    out.sort(key=lambda z: (z.created_at, z.type, z.price_low))
    return out


# ── merge: forward-only ───────────────────────────────────────────────────────
def _rank(z: Zone) -> int:
    if z.state == "void":
        return VOID_RANK
    return RANK.get(z.type, {}).get(z.state, 0)


def merge_forward(old: Zone, new: Zone) -> Zone:
    """Set-once timestamps; state rank never decreases; a void zone stays void."""
    if not config.rule("zones", "forward_only", True):
        return new
    m = Zone(**{**old.to_dict(), "extra": dict(old.extra)})
    for k in ("mitigated_at", "invalidated_at", "source_event_id"):
        if getattr(m, k) is None and getattr(new, k) is not None:
            setattr(m, k, getattr(new, k))
    for k, v in new.extra.items():
        if k.endswith("_at"):
            m.extra.setdefault(k, v)
        else:
            m.extra[k] = v
    if old.state != "void" and _rank(new) > _rank(old):
        m.state = new.state
    m.label = new.label or old.label
    return m


# ── store ─────────────────────────────────────────────────────────────────────
def _row_to_zone(r) -> Zone:
    p = json.loads(r["payload"] or "{}")
    return Zone(r["id"], r["type"], r["tf"], r["side"], r["low"], r["high"], r["created_at"], r["state"],
                r["mitigated_at"], r["invalidated_at"], r["source_event_id"], p.get("label", ""), p.get("extra", {}))


def load(tf: str, since: int = 0) -> Dict[str, Zone]:
    rows = db.query("SELECT * FROM zones WHERE tf=? AND created_at>=? ORDER BY created_at", (tf, since))
    return {r["id"]: _row_to_zone(r) for r in rows}


def _save(zs: Iterable[Zone]) -> None:
    now = int(time.time())
    db.execmany("INSERT OR REPLACE INTO zones (id, tf, type, side, low, high, created_at, mitigated_at, "
                "invalidated_at, state, source_event_id, payload, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(z.id, z.timeframe, z.type, z.side, z.price_low, z.price_high, z.created_at, z.mitigated_at,
                  z.invalidated_at, z.state, z.source_event_id, json.dumps({"label": z.label, "extra": z.extra}), now)
                 for z in zs])


def reconcile(tf: str, fresh: List[Zone], window: tuple, after_repair: bool = False,
              guard_s: int = 0, persist: Optional[bool] = None) -> List[Zone]:
    """Merge a recomputation into the stored registry. Returns the merged zones of this window (+ voided)."""
    persist = config.rule("zones", "persist", True) if persist is None else persist
    if not persist:
        return fresh
    w0, w1 = int(window[0]), int(window[1])
    stored = load(tf, w0 - 86400)
    seen, out, changed = set(), [], []
    for z in fresh:
        s = stored.get(z.id)
        m = z if s is None else merge_forward(s, z)
        if s is None or m.to_dict() != s.to_dict():
            changed.append(m)
        seen.add(z.id)
        out.append(m)
    if after_repair and config.rule("zones", "void_missing_after_repair", True):
        for zid, s in stored.items():
            if zid in seen or s.state == "void":
                continue
            if w0 + guard_s <= s.created_at <= w1:
                s.state = "void"
                s.extra["void_reason"] = "candles corrected by REST backfill no longer produce this zone"
                changed.append(s)
                out.append(s)
    if changed:
        _save(changed)
    return out


def prune(keep_days: Optional[int] = None) -> int:
    keep = int(keep_days if keep_days is not None else config.rule("zones", "keep_days", 10)) * 86400
    return db.execute("DELETE FROM zones WHERE created_at < ?", (int(time.time()) - keep,)).rowcount


def at_price(zones: List[Zone], bar_ts: int, lo: float, hi: float, tol: float) -> List[Zone]:
    """Active zones that the candle [lo, hi] overlaps (levels within tol). Strongest kinds first."""
    pr = {"ORDER_BLOCK": 0, "FVG": 1, "LIQ_POOL": 2}
    hit = [z for z in zones if z.active_for(bar_ts) and lo <= z.price_high + tol and hi >= z.price_low - tol]
    return sorted(hit, key=lambda z: (pr.get(z.type, 9), -z.created_at))
