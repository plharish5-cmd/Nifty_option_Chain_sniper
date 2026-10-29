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
  WALL      = a level holding ≥ wall_mult × the median level size for ≥ 15 s
"""
from __future__ import annotations

import struct
from typing import Any, Dict, List

from fastapi import APIRouter

from ..dhan import feed as feed_mod
from ..engine.orderbook import ORDERBOOK as BOOK, OrderBook as Book

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


# ── 2. hook the hub's Full-packet callback ────────────────────────────────────
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
