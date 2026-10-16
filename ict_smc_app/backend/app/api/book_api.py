"""
api/book_api.py — NIFTY FUT order book: book balance + 5-level DOM ladder with PULLED / RELOAD events.
Display only — never places orders. Uses the Full packets the desk ALREADY receives (no extra Dhan connection).

Hooks (no edits to existing files):
  • dhan.feed.decode  → also returns all 5 depth levels as p["depth"]
  • Hub.on_full       → also feeds BOOK.on_packet()
Definitions
  balance   = total buy qty ÷ (total buy + total sell) waiting in the whole book (Dhan "total buy/sell qty")
  PULLED    = a level's resting size drops ≥ 50 % and ≥ pull_min while (almost) nothing traded at that price
              → the order was cancelled: that support / resistance was not real (or is being moved)
  RELOAD    = size traded at a price and, within 5 s, the level refills to ≥ 80 % of what it had → a big
              player keeps defending that price (iceberg-LIKE behaviour; NSE does not tag real icebergs)
  WALL      = a level holding ≥ wall_mult × the median level size for ≥ 20 s
"""
from __future__ import annotations

import struct
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from fastapi import APIRouter

from .. import config
from ..dhan import feed as feed_mod
from ..timeutil import ist_date

router = APIRouter()
_DEPTH = struct.Struct("<iihhff")


# ── 1. decode all 5 depth levels (wraps the existing decoder) ─────────────────
_orig_decode = feed_mod.decode


def _decode(buf: bytes) -> List[dict]:
    out = _orig_decode(buf)
    try:
        i = 0
        for p in out:                                       # re-walk the buffer in the same packet order
            code, length, _seg, _sid = feed_mod.HDR.unpack_from(buf, i)
            if length < feed_mod.HDR.size or i + length > len(buf):
                length = len(buf) - i
            if code == 8 and i + 62 + 100 <= len(buf):
                lv = []
                for k in range(5):
                    bq, aq, bo, ao, bp, ap = _DEPTH.unpack_from(buf, i + 62 + 20 * k)
                    lv.append((round(bp, 2), bq, bo, round(ap, 2), aq, ao))
                p["depth"] = lv
            i += length
    except Exception:                                        # noqa: BLE001 — never break the feed
        pass
    return out


feed_mod.decode = _decode


# ── 2. the book state ─────────────────────────────────────────────────────────
class Book:
    def __init__(self) -> None:
        self.reset("")

    def reset(self, day: str) -> None:
        self.day = day
        self.ts = 0.0
        self.tot_buy = self.tot_sell = 0
        self.ladder: List[tuple] = []
        self.prev: Dict[tuple, int] = {}                   # (side, price) → qty at the previous packet
        self.hist: Dict[tuple, Deque] = {}                 # (side, price) → deque[(t, qty)] for walls / reloads
        self.traded: Dict[float, List[float]] = {}         # price → [buy qty, sell qty] today
        self.last_vol: Optional[int] = None
        self.last_px: Optional[float] = None
        self.hit_at: Dict[tuple, tuple] = {}               # (side, price) → (t, qty before the hit) for reloads
        self.events: Deque[dict] = deque(maxlen=300)
        self.minutes: Dict[int, List[float]] = {}          # minute ts → [sum of balance, n]
        self.packets = 0

    def on_packet(self, p: Dict[str, Any], t: float) -> None:
        dep = p.get("depth")
        if not dep:
            return
        d = ist_date(t).isoformat()
        if d != self.day:
            self.reset(d)
        self.packets += 1
        self.ts = t
        lot = int(config.rule("paper", "lot_size", 65))
        pull_min = float(config.rule("book", "pull_min_lots", 20)) * lot
        tb, ts_ = int(p.get("tot_buy") or 0), int(p.get("tot_sell") or 0)
        if tb + ts_ > 0:
            self.tot_buy, self.tot_sell = tb, ts_
            m = int(t // 60 * 60)
            acc = self.minutes.setdefault(m, [0.0, 0])
            acc[0] += tb / (tb + ts_); acc[1] += 1
        # traded volume at the last price (aggressor by quote rule against the previous ladder)
        vol, px = p.get("volume"), p.get("ltp")
        dv = 0.0
        if vol is not None and self.last_vol is not None and vol > self.last_vol and px:
            dv = float(vol - self.last_vol)
            best_ask = self.ladder[0][3] if self.ladder else None
            best_bid = self.ladder[0][0] if self.ladder else None
            side = 0 if best_ask and px >= best_ask else 1 if best_bid and px <= best_bid else (0 if self.last_px and px > self.last_px else 1)
            self.traded.setdefault(round(px, 2), [0.0, 0.0])[side] += dv
        if vol is not None:
            self.last_vol = vol
        if px:
            self.last_px = px
        # level changes → PULLED / RELOAD
        cur: Dict[tuple, int] = {}
        for bp, bq, _bo, ap, aq, _ao in dep:
            if bp > 0:
                cur[("B", bp)] = bq
            if ap > 0:
                cur[("A", ap)] = aq
        best_b = max((k[1] for k in cur if k[0] == "B"), default=None)
        best_a = min((k[1] for k in cur if k[0] == "A"), default=None)
        for k, q0 in self.prev.items():
            q1 = cur.get(k)
            if q1 is None:
                # level left the visible 5 — only judge it if price is still inside the range we can see
                inside = best_b is not None and best_a is not None and (
                    (k[0] == "B" and k[1] < best_b and k[1] > best_b - 5 * 0.05) or (k[0] == "A" and k[1] > best_a and k[1] < best_a + 5 * 0.05))
                if not inside:
                    continue
                q1 = 0
            drop = q0 - q1
            traded_here = dv if px and abs(px - k[1]) < 1e-6 else 0.0
            if drop >= pull_min and drop >= 0.5 * q0 and traded_here < 0.2 * drop:
                self._ev(t, k, "PULLED", drop, f"{drop / lot:.0f} lots pulled from {'bid' if k[0] == 'B' else 'ask'} {k[1]:.2f} without trading — that {'support' if k[0] == 'B' else 'resistance'} was not real")
            elif traded_here >= 0.3 * q0 and q0 >= pull_min:
                self.hit_at[k] = (t, q0)
        for k, (th, qh) in list(self.hit_at.items()):
            if t - th > 5:
                del self.hit_at[k]; continue
            if cur.get(k, 0) >= 0.8 * qh and t - th > 0.5:
                self._ev(t, k, "RELOAD", cur[k], f"{'bid' if k[0] == 'B' else 'ask'} {k[1]:.2f} was hit and refilled to {cur[k] / lot:.0f} lots — someone is DEFENDING it ({'support' if k[0] == 'B' else 'resistance'})")
                del self.hit_at[k]
        # walls (held ≥ 20 s at ≥ wall_mult × median visible level)
        sizes = sorted(cur.values())
        med = sizes[len(sizes) // 2] if sizes else 0
        mult = float(config.rule("book", "wall_mult", 3.0))
        for k, q in cur.items():
            h = self.hist.setdefault(k, deque(maxlen=120))
            h.append((t, q))
            if med and q >= mult * med and h[0][0] <= t - 20 and all(x[1] >= 0.7 * q for x in h if x[0] >= t - 20):
                if not any(e["kind"] == "WALL" and e["price"] == k[1] and t - e["ts"] < 60 for e in self.events):
                    self._ev(t, k, "WALL", q, f"{q / lot:.0f} lots resting at {'bid' if k[0] == 'B' else 'ask'} {k[1]:.2f} for 20 s+ ({q / med:.1f}× a normal level) — real {'support' if k[0] == 'B' else 'resistance'} until it's pulled")
        self.prev = cur
        self.ladder = dep

    def _ev(self, t: float, k: tuple, kind: str, qty: float, why: str) -> None:
        self.events.append({"ts": int(t), "price": round(k[1], 2), "side": "bid" if k[0] == "B" else "ask", "kind": kind, "qty": int(qty), "why": why})

    def view(self) -> Dict[str, Any]:
        lot = int(config.rule("paper", "lot_size", 65))
        if not self.packets:
            return {"available": False, "message": "no NIFTY FUT full packets yet (market closed, or futures feed not subscribed)"}
        tot = self.tot_buy + self.tot_sell
        bp = round(100 * self.tot_buy / tot, 1) if tot else None
        ser = [{"ts": m, "bp": round(100 * s / n, 1)} for m, (s, n) in sorted(self.minutes.items()) if n]
        b10 = [x["bp"] for x in ser[-10:]]
        trend = round(b10[-1] - b10[0], 1) if len(b10) >= 2 else 0.0
        rows = []
        for bpx, bq, bo, apx, aq, ao in self.ladder:
            rows.append({"bid": bpx, "bid_qty": bq, "bid_orders": bo, "ask": apx, "ask_qty": aq, "ask_orders": ao,
                         "bid_traded": self.traded.get(bpx, [0, 0]), "ask_traded": self.traded.get(apx, [0, 0])})
        sb = sum(r["bid_qty"] for r in rows); sa = sum(r["ask_qty"] for r in rows)
        return {"available": True, "ts": int(self.ts), "lot": lot,
                "balance": {"buy_pct": bp, "tot_buy": self.tot_buy, "tot_sell": self.tot_sell, "trend10": trend,
                            "top5_buy_pct": round(100 * sb / (sb + sa), 1) if sb + sa else None},
                "series": ser[-400:], "ladder": rows, "events": list(self.events)[-60:][::-1],
                "spread": round(rows[0]["ask"] - rows[0]["bid"], 2) if rows else None}


BOOK = Book()


# ── 3. hook the hub's Full-packet callback ────────────────────────────────────
from .hub import Hub  # noqa: E402

_orig_full = Hub.on_full


async def _on_full(self, p, t):
    await _orig_full(self, p, t)
    try:
        BOOK.on_packet(p, t)
    except Exception:                                        # noqa: BLE001
        pass


Hub.on_full = _on_full


@router.get("/api/book")
def book():
    return BOOK.view()
