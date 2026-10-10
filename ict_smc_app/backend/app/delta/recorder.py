"""
delta/recorder.py — ESTIMATED delta and footprint from the live NIFTY future (Dhan Full packets).

Dhan does not tag the aggressor of a trade. What a Full packet does carry: last price, cumulative day volume and the
best bid / ask. Between two packets, the volume that printed (Δ cumulative volume) is classified like this —
the classic Lee-Ready idea:
    price ≥ the PREVIOUS packet's ask   → buyers lifted the offer      (quote rule)
    price ≤ the PREVIOUS packet's bid   → sellers hit the bid          (quote rule)
    in between                           → up-tick = buy, down-tick = sell, unchanged = same as the last tick
                                                                         (tick rule)
    nothing to compare yet               → unclassified (counted, never guessed)

HONEST LIMITS — shown on the status line and labelled "Estimated" everywhere:
  * Dhan streams SNAPSHOTS, not every trade: several trades between two packets are lumped into one print.
  * History starts the minute the recorder starts. There is no backfill of delta (Dhan has none).
  * Prices are mapped to the INDEX by subtracting the live basis (future − spot) at that moment, so the
    footprint levels sit on the NIFTY chart, in `delta.fut_level_step`-point rows.

It also builds the future's own 1-minute OHLCV from the same packets, so volume is ready the moment a minute
closes; the REST candles fetched a few seconds later overwrite it with Dhan's official numbers.

Stored in delta_bars / footprint_levels with inst = "FUTQR" (src "fut_quote_rule") — read by FutQuoteRuleAdapter.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional

from .. import config, db
from ..market import futures as fut_mod
from ..timeutil import bucket_start, ist_date

log = logging.getLogger("recorder")

INST = "FUTQR"
SRC = "fut_quote_rule"


class FutRecorder:
    def __init__(self, spot: Callable[[], Optional[float]]) -> None:
        self.spot = spot
        self.reset()
        self.packets = 0
        self.counts = {"quote": 0.0, "tick": 0.0, "unclassified": 0.0}
        self.first_ts: Optional[int] = None

    def reset(self) -> None:
        self.prev_vol: Optional[float] = None
        self.prev_bid: Optional[float] = None
        self.prev_ask: Optional[float] = None
        self.last_px: Optional[float] = None
        self.last_side: Optional[str] = None
        self.basis: Optional[float] = None
        self.day: Optional[str] = None
        self.min: Dict[int, Dict[str, Any]] = {}             # minute → {"lv": {px: [buy, sell]}, buy, sell, ohlcv}

    # ── packets ──────────────────────────────────────────────────────────
    def on_packet(self, p: Dict[str, Any], t: float) -> Optional[str]:
        """Returns the side it classified ('buy' / 'sell' / None)."""
        self.packets += 1
        vol, px = p.get("volume"), p.get("ltp")
        if vol is None or not px:
            return None
        d = ist_date(t).isoformat()
        if d != self.day:
            self.reset()
            self.day = d
        side = None
        m = bucket_start(t, "1m")
        rec = self.min.setdefault(m, {"lv": defaultdict(lambda: [0.0, 0.0]), "buy": 0.0, "sell": 0.0, "unc": 0.0,
                                      "o": px, "h": px, "l": px, "c": px, "v": 0.0})
        rec["h"], rec["l"], rec["c"] = max(rec["h"], px), min(rec["l"], px), px
        if p.get("oi"):
            rec["oi"] = float(p["oi"])
        if self.prev_vol is not None and vol > self.prev_vol:
            dv = float(vol - self.prev_vol)
            rec["v"] += dv
            how = "tick"
            if self.prev_ask is not None and px >= self.prev_ask:
                side, how = "buy", "quote"
            elif self.prev_bid is not None and px <= self.prev_bid:
                side, how = "sell", "quote"
            elif self.last_px is not None and px > self.last_px:
                side = "buy"
            elif self.last_px is not None and px < self.last_px:
                side = "sell"
            else:
                side = self.last_side
            spot = self.spot()
            if side is None or not spot:
                self.counts["unclassified"] += dv
                rec["unc"] += dv
            else:
                self.counts[how] += dv
                step = float(config.rule("delta", "fut_level_step", 5.0))
                raw = px - spot                               # live basis, smoothed so tick noise between the
                if abs(raw) < 0.05 * spot:                    # two feeds doesn't smear the levels
                    self.basis = raw if self.basis is None else self.basis + 0.05 * (raw - self.basis)
                lvl = round(((px - self.basis) // step) * step, 2) if self.basis is not None else None
                if lvl is None:
                    self.counts["unclassified"] += dv
                    rec["unc"] += dv
                else:
                    rec["lv"][lvl][0 if side == "buy" else 1] += dv
                    rec["buy" if side == "buy" else "sell"] += dv
                    if self.first_ts is None:
                        self.first_ts = m
        elif self.prev_vol is not None and vol < self.prev_vol:   # a new session's counter: start over
            self.reset()
            self.day = d
        self.prev_vol = float(vol)
        if px != self.last_px and side is not None:
            self.last_side = side
        if self.last_px is None or px != self.last_px:
            self.last_px = px
        if p.get("bid") and p.get("ask"):
            self.prev_bid, self.prev_ask = float(p["bid"]), float(p["ask"])
        return side

    # ── minute close ─────────────────────────────────────────────────────
    def close_minute(self, m: int) -> Optional[Dict[str, Any]]:
        """Persist minute m (and anything older still in memory). Returns minute m's record summary."""
        out = None
        for k in sorted(x for x in self.min if x <= m):
            r = self.min.pop(k)
            if r["buy"] + r["sell"] > 0:
                db.execute("INSERT OR REPLACE INTO delta_bars (inst, ts, secs, buy, sell, delta, src) VALUES (?,?,?,?,?,?,?)",
                           (INST, k, 60, r["buy"], r["sell"], r["buy"] - r["sell"], SRC))
                db.execute("DELETE FROM footprint_levels WHERE inst=? AND ts=?", (INST, k))
                db.execmany("INSERT OR REPLACE INTO footprint_levels (inst, ts, price, buy, sell) VALUES (?,?,?,?,?)",
                            [(INST, k, p, bv, sv) for p, (bv, sv) in r["lv"].items()])
            if r["v"] > 0:                                   # provisional FUT candle; REST overwrites it later
                have = db.query("SELECT 1 FROM candles_1m WHERE sec_id=? AND ts=?", (fut_mod.SEC, k))
                if not have:
                    db.upsert_candles(fut_mod.SEC, [{"ts": k, "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"], "v": r["v"]}])
            if r.get("oi"):                                  # provisional OI; REST (oi=True) replaces it
                db.execute("INSERT OR IGNORE INTO fut_oi (ts, oi, src) VALUES (?,?,?)", (k, r["oi"], "feed"))
            if k == m:
                out = {"ts": k, "buy": r["buy"], "sell": r["sell"], "unclassified": r["unc"], "v": r["v"]}
        return out

    def status(self) -> Dict[str, Any]:
        tot = sum(self.counts.values())
        share = {k: round(100 * v / tot, 1) for k, v in self.counts.items()} if tot else {}
        return {"packets": self.packets, "classified_share": share, "since": self.first_ts}


def stored_span() -> Dict[str, Any]:
    r = db.query("SELECT COUNT(*) n, MIN(ts) a, MAX(ts) b FROM delta_bars WHERE inst=?", (INST,))[0]
    return {"minutes": r["n"] or 0, "first": r["a"], "last": r["b"]}
