"""
setup_log_api.py — /api/setups : record and read back the setups OcLab fires. Display only — never orders.

Why this exists. SNIPER BREAKDOWN / PULLBACK SELL / DIP BUY, the conviction percentage, Scissor and
Synergy are all computed in the browser (OcLab.tsx) and were never stored anywhere. So there has been no
record of which setup fired, at what price, with what stop and target — and therefore no way to ask the
only question that matters: does a 95% badge win more often than a 35% one?

This is that record. It stores what the browser already decided; it does not re-derive or second-guess it,
because a second implementation would drift from the one you actually trade. Scoring the outcomes comes
later, from candles, once there are sessions to score.

`key` is UNIQUE. OcLab re-renders on every poll, so the same setup is posted repeatedly and the duplicates
are dropped by INSERT OR IGNORE rather than by trusting the client to remember what it already sent.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter
from pydantic import BaseModel, Field

from .. import db
from ..timeutil import ist_date, now_ist

log = logging.getLogger(__name__)
router = APIRouter()

MAX_BATCH = 50          # one poll cannot legitimately produce more setups than this


class SetupIn(BaseModel):
    ts: int = Field(0, description="epoch seconds of the bar; 0 means resolve hhmm against today (IST)")
    hhmm: str = Field("", max_length=5, description="bar time as the browser has it — it holds no epoch")
    key: str = Field(..., max_length=160, description="stable id: ts|setup|side|entry — dedupes re-posts")
    setup: str = Field("", max_length=64)
    side: str = Field("", max_length=16)
    entry: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    conviction_pct: Optional[int] = None
    conviction_band: str = Field("", max_length=32)
    synergy: bool = False
    scissor: bool = False
    spot: Optional[float] = None
    payload: Optional[Dict[str, Any]] = None


class SetupBatch(BaseModel):
    setups: List[SetupIn] = Field(default_factory=list)


def _sanitize_sl(entry: Optional[float], sl: Optional[float], side: str, payload: Optional[Dict[str, Any]]) -> Optional[float]:
    if entry is None or sl is None:
        return sl
    # Fix truncated leading digit: e.g. entry = 22828.5, sl = 2796.0 -> 22796.0
    if entry > 15000 and 0 < sl < 10000:
        prefix = int(entry // 10000) * 10000
        candidate_sl = prefix + sl
        if 5 <= abs(entry - candidate_sl) <= 500:
            return round(candidate_sl, 1)
        if payload and isinstance(payload, dict) and payload.get("risk_pts"):
            try:
                risk_pts = float(payload["risk_pts"])
                reconstructed = entry - risk_pts if side in ("bull", "buy") else entry + risk_pts
                return round(reconstructed, 1)
            except (ValueError, TypeError):
                pass
    return round(sl, 1)


def _ensure_tp(entry: Optional[float], sl: Optional[float], tp: Optional[float], side: str, setup: str) -> Optional[float]:
    """Record the target the trader was SHOWN, or nothing.

    This used to compute `entry +/- 2R` whenever tp arrived empty. That writes a number nobody ever
    saw into the one table whose purpose is to answer "what did the desk actually say, and was it
    right?" -- 29 Oct 09:57 SNIPER BREAKOUT displayed "(35pt risk)" with no target at all, and was
    stored as tp 22815.2. It also silently overrode the clamp: a ticket shown "Tgt 22867 (capped at
    session high)" would be recorded with a raw 2R extension instead of the capped figure the trader
    traded off, making every R-multiple computed from this table fiction.

    Empty is now kept empty. A ticket with no target is a real finding about the signal, not a gap to
    fill -- and since every sniper now ships a 2R target client-side, a NULL here flags an old row or
    a genuine bug rather than something to paper over.
    """
    return None if tp is None else round(tp, 1)


def _resolve_ts(s: "SetupIn") -> int:
    """The browser's rows carry only "HH:MM" — no epoch — so reconstructing the date there means doing
    timezone arithmetic in the client, which silently breaks around midnight and on a replayed session.
    Resolve it here instead, against today's IST date, and fall back to now if the time is unparseable
    rather than storing a nonsense timestamp."""
    if s.ts:
        return int(s.ts)
    now = now_ist()
    try:
        hh, mm = (int(x) for x in s.hhmm.split(":", 1))
        return int(now.replace(hour=hh, minute=mm, second=0, microsecond=0).timestamp())
    except (ValueError, TypeError):
        log.warning("setup_log: unparseable hhmm %r for key=%s, stamping now", s.hhmm, s.key)
        return int(now.timestamp())


@router.post("/api/setups/log")
def log_setups(batch: SetupBatch) -> Dict[str, Any]:
    rows = batch.setups[:MAX_BATCH]
    if not rows:
        return {"ok": True, "stored": 0, "received": 0}

    # A CONFLICT or RE-FIRE row used to DELETE the SNIPER ticket in its bucket. That erased the very
    # thing suppression has to be judged by: with the original gone, "did standing aside cost a good
    # trade?" has no answer, and the arbitration becomes an unmeasurable veto. It was also deleting by
    # bucket+setup regardless of side, so an unrelated ticket in the same bucket went with it.
    #
    # The premature rows it was cleaning up (e.g. 29 Oct 10:00, where both a SNIPER BREAKDOWN and the
    # CONFLICT that demoted it were stored) can no longer be written at all: the client posts closed
    # bars only, so a bar is logged once, in its final state. Nothing is deleted here any more.
    stored = 0
    seen_buckets: set[Tuple[int, str, str]] = set()

    for s in rows:
        try:
            s.ts = _resolve_ts(s)
            # One bar is one signal, so the bar's own ts IS the bucket. The previous (ts // 180)
            # hardcoded a 3-minute bar: on the 1m timeframe three consecutive bars fall in one bucket,
            # so two real signals out of every three were silently dropped. Bucketing on the exact ts
            # is timeframe-agnostic and matches the client key (ts|setup|side) exactly.
            b_bucket = int(s.ts)
            clean_setup = s.setup.strip()
            clean_side = s.side.strip()

            # Deduplicate per bar for a given (setup, side)
            bucket_key = (b_bucket, clean_setup, clean_side)
            if bucket_key in seen_buckets:
                continue

            # Check if canonical row already exists in database for this 3m bucket
            existing = db.query(
                "SELECT 1 FROM setup_log WHERE ts = ? AND setup = ? AND side = ? LIMIT 1",
                (b_bucket, clean_setup, clean_side),
            )
            if existing:
                seen_buckets.add(bucket_key)
                continue

            seen_buckets.add(bucket_key)

            # Sanitize SL and ensure TP
            s.sl = _sanitize_sl(s.entry, s.sl, clean_side, s.payload)
            s.tp = _ensure_tp(s.entry, s.sl, s.tp, clean_side, clean_setup)

            cur = db.execute(
                "INSERT OR IGNORE INTO setup_log "
                "(ts,key,d,setup,side,entry,sl,tp,conviction_pct,conviction_band,synergy,scissor,spot,payload) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (int(s.ts), s.key, ist_date(int(s.ts)).isoformat(), s.setup, s.side, s.entry, s.sl, s.tp,
                 s.conviction_pct, s.conviction_band, int(s.synergy), int(s.scissor), s.spot,
                 json.dumps(s.payload) if s.payload else None))
            stored += int(cur.rowcount or 0)
        except Exception:                                    # noqa: BLE001
            log.exception("setup_log insert failed for key=%s", s.key)
    return {"ok": True, "stored": stored, "received": len(rows)}


@router.get("/api/setups")
def read_setups(d: str = "", limit: int = 500) -> Dict[str, Any]:
    """Setups for one IST date (default: today). Newest first."""
    day = d or ist_date(int(time.time())).isoformat()
    limit = max(1, min(int(limit), 5000))
    rows = db.query(
        "SELECT ts,key,d,setup,side,entry,sl,tp,conviction_pct,conviction_band,synergy,scissor,spot "
        "FROM setup_log WHERE d=? ORDER BY ts DESC LIMIT ?", (day, limit))
    return {"ok": True, "date": day, "count": len(rows), "setups": [dict(r) for r in rows]}


@router.get("/api/setups/days")
def read_days() -> Dict[str, Any]:
    """How many sessions of setups have accumulated — i.e. whether there is yet enough to measure."""
    rows = db.query("SELECT d, COUNT(*) AS n FROM setup_log GROUP BY d ORDER BY d DESC LIMIT 400")
    days = [dict(r) for r in rows]
    return {"ok": True, "sessions": len(days), "total": sum(int(x["n"]) for x in days), "days": days}
