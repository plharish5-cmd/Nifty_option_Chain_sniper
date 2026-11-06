"""
volume_store.py — one shared source of truth for NIFTY futures volume.

WHY
---
Futures volume was the ONLY measure of seventeen in scripts/backtest_ma_geometry.py
with a positive directional advantage (+Rs262/trade): shorting the same bars did
NOT also beat its baseline, which is the one control a lagging indicator cannot
fake. Every moving-average transformation failed it. So this is the one series
worth protecting and growing.

It is not yet a tradeable edge. Out-of-sample n was 117 over roughly 20 sessions
from a single two-month regime, and once the top-5 concentration and 0.25pt/leg
slippage are applied it comes to about +Rs4/trade. The honest position is: keep
recording, re-test at ~120 sessions.

WHAT THIS GIVES YOU
-------------------
- coverage():   which sessions exist, how complete, where the gaps are
- slot_vsr():   volume surge vs the SAME CLOCK-TIME SLOT on prior sessions
- combined():   front + next month summed, so monthly rollover does not read as
                a volume collapse

CRITICAL: VSR IS SLOT-NORMALISED, NOT A FLAT SMA
------------------------------------------------
NIFTY volume is strongly U-shaped across the day — heavy at the open, dead at
midday, heavy into the close. volume[t] / SMA(volume, 10) therefore mostly
measures what time it is. Comparing a 12:45 bar against prior sessions' 12:45
bars removes that entirely. This is the method already used in rvol_gate.py and
auction.py; the naive form was only ever in the backtest spec.

Pure functions take rows and return numbers, so the maths is testable with no
database, no Dhan token and no market session.

Python 3.9 compatible: no PEP 604 unions.
"""
from __future__ import annotations

import logging
import statistics
from collections import defaultdict
from typing import Any, Dict, List, Optional, Sequence, Tuple

log = logging.getLogger("desk.volume_store")

IST_SQL = "'+5 hours','+30 minutes'"
SESSION_MINUTES = 375  # 09:15 -> 15:30

FRONT = "FUT"
NEXT = "FUT2"


# ─────────────────────────────────────────────────────────────────────────────
# PURE MATHS
# ─────────────────────────────────────────────────────────────────────────────

def slot_key(session_time: str, tf_minutes: int) -> str:
    """Bucket a HH:MM:SS into its timeframe slot, e.g. 12:47 at tf=15 -> '12:45'."""
    try:
        hh, mm = int(session_time[0:2]), int(session_time[3:5])
    except (ValueError, IndexError):
        return "00:00"
    total = hh * 60 + mm
    bucket = (total // max(1, tf_minutes)) * max(1, tf_minutes)
    return "{:02d}:{:02d}".format(bucket // 60, bucket % 60)


def combined(front_rows: Sequence[Dict[str, Any]],
             next_rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Front + next month volume summed per timestamp.

    In the days before monthly expiry, volume migrates from the front contract to
    the next one. A front-only series therefore shows a volume collapse every
    month that is pure rollover, not a real drop in participation. Summing both
    removes it.
    """
    merged: Dict[int, Dict[str, Any]] = {}
    for rows in (front_rows, next_rows):
        for r in rows or []:
            ts = int(r["ts"])
            cur = merged.get(ts)
            if cur is None:
                merged[ts] = {"ts": ts, "volume": float(r.get("volume") or 0.0),
                              "session_date": r.get("session_date"),
                              "session_time": r.get("session_time")}
            else:
                cur["volume"] += float(r.get("volume") or 0.0)
    return [merged[k] for k in sorted(merged)]


def build_slot_baseline(rows: Sequence[Dict[str, Any]], tf_minutes: int,
                        exclude_sessions: Optional[Sequence[str]] = None
                        ) -> Dict[str, List[float]]:
    """{slot -> [per-session total volume in that slot]}, oldest first.

    exclude_sessions drops expiry days from the baseline: option expiry sessions
    carry abnormal futures volume and would inflate the normal level for every
    other day.
    """
    skip = set(exclude_sessions or ())
    per_session: Dict[Tuple[str, str], float] = defaultdict(float)
    order: Dict[str, List[str]] = defaultdict(list)
    for r in rows or []:
        day = r.get("session_date")
        if not day or day in skip:
            continue
        slot = slot_key(str(r.get("session_time") or ""), tf_minutes)
        key = (slot, day)
        if key not in per_session:
            order[slot].append(day)
        per_session[key] += float(r.get("volume") or 0.0)
    return {slot: [per_session[(slot, d)] for d in days] for slot, days in order.items()}


def slot_vsr(current_volume: float, slot_history: Sequence[float],
             min_sessions: int = 5) -> Dict[str, Any]:
    """Volume surge ratio against the same clock slot on prior sessions.

    Returns ratio, the median baseline, a robust z-score and the sample size.
    Median rather than mean, because a single expiry-day spike in the baseline
    would otherwise suppress every subsequent reading.

    Returns ok=False when there is not enough history — never a fabricated 1.0.
    """
    hist = [float(v) for v in (slot_history or []) if v and v > 0]
    if len(hist) < max(1, min_sessions):
        return {"ok": False, "reason": "only {} baseline session(s), need {}".format(
            len(hist), min_sessions), "n_baseline": len(hist),
            "ratio": None, "z": None, "median": None}
    med = statistics.median(hist)
    if med <= 0:
        return {"ok": False, "reason": "baseline median is zero",
                "n_baseline": len(hist), "ratio": None, "z": None, "median": med}
    ratio = float(current_volume) / med
    z = None
    if len(hist) >= 3:
        try:
            sd = statistics.pstdev(hist)
            if sd > 0:
                z = (float(current_volume) - med) / sd
        except statistics.StatisticsError:
            z = None
    return {"ok": True, "ratio": round(ratio, 3), "median": round(med, 1),
            "z": round(z, 2) if z is not None else None,
            "n_baseline": len(hist), "reason": ""}


def classify_vsr(ratio: Optional[float], thresholds: Optional[Dict[str, float]] = None
                 ) -> str:
    """Label a surge ratio. Thresholds are caller-supplied (config), not baked in."""
    if ratio is None:
        return "UNKNOWN"
    t = thresholds or {}
    if ratio >= float(t.get("surge", 1.8)):
        return "SURGE"
    if ratio >= float(t.get("elevated", 1.2)):
        return "ELEVATED"
    if ratio <= float(t.get("dry", 0.6)):
        return "DRY"
    return "NORMAL"


# ─────────────────────────────────────────────────────────────────────────────
# COVERAGE — what we actually hold, and where the holes are
# ─────────────────────────────────────────────────────────────────────────────

def coverage_from_rows(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Per-session completeness. A session is 375 one-minute bars.

    Reported honestly: a desk that was asleep has gaps, and a backtest built on
    a third of the sessions should say so rather than quietly average over them.
    """
    per_day: Dict[str, int] = defaultdict(int)
    first_last: Dict[str, List[str]] = {}
    for r in rows or []:
        day = r.get("session_date")
        if not day:
            continue
        per_day[day] += 1
        t = str(r.get("session_time") or "")
        if day not in first_last:
            first_last[day] = [t, t]
        else:
            if t < first_last[day][0]:
                first_last[day][0] = t
            if t > first_last[day][1]:
                first_last[day][1] = t
    days = sorted(per_day)
    sessions = [{"date": d, "minutes": per_day[d],
                 "coverage_pct": round(100.0 * per_day[d] / SESSION_MINUTES, 1),
                 "first": first_last[d][0], "last": first_last[d][1]} for d in days]
    total = sum(per_day.values())
    return {
        "sessions": sessions,
        "n_sessions": len(days),
        "first_session": days[0] if days else None,
        "last_session": days[-1] if days else None,
        "total_minutes": total,
        "mean_coverage_pct": round(
            100.0 * total / (len(days) * SESSION_MINUTES), 1) if days else 0.0,
        "partial_sessions": [s["date"] for s in sessions if s["coverage_pct"] < 80.0],
        "_note": "A full session is 375 one-minute bars. Sessions below 80% are "
                 "listed: the recorder only runs while the desk is on.",
    }


# ─────────────────────────────────────────────────────────────────────────────
# DB ACCESS (thin; everything above is pure)
# ─────────────────────────────────────────────────────────────────────────────

def load_rows(conn, sec_id: str, since_ts: Optional[int] = None) -> List[Dict[str, Any]]:
    """Read one instrument's 1-minute volume series out of candles_1m."""
    sql = ("SELECT ts, v AS volume, "
           "date(ts,'unixepoch',{tz}) AS session_date, "
           "time(ts,'unixepoch',{tz}) AS session_time "
           "FROM candles_1m WHERE sec_id = ?").format(tz=IST_SQL)
    args: List[Any] = [sec_id]
    if since_ts:
        sql += " AND ts >= ?"
        args.append(int(since_ts))
    sql += " ORDER BY ts ASC"
    try:
        cur = conn.execute(sql, args)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as exc:
        log.warning("volume_store: load_rows(%s) failed: %s", sec_id, exc)
        return []


def current_surge(conn, tf_minutes: int = 15, lookback_sessions: int = 20,
                  use_combined: bool = True,
                  thresholds: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """Volume surge ratio for the most recent completed slot.

    Fails closed: if there is no data, or not enough baseline sessions, it says
    so rather than returning a confident 1.0.
    """
    front = load_rows(conn, FRONT)
    nxt = load_rows(conn, NEXT) if use_combined else []
    rows = combined(front, nxt) if use_combined else front
    if not rows:
        return {"ok": False, "reason": "no futures volume recorded",
                "coverage": coverage_from_rows([])}

    cov = coverage_from_rows(rows)
    days = [s["date"] for s in cov["sessions"]]
    recent = set(days[-(lookback_sessions + 1):])
    rows = [r for r in rows if r.get("session_date") in recent]

    latest_day = days[-1]
    today_rows = [r for r in rows if r.get("session_date") == latest_day]
    if not today_rows:
        return {"ok": False, "reason": "no rows on latest session", "coverage": cov}

    latest_slot = slot_key(str(today_rows[-1].get("session_time") or ""), tf_minutes)
    cur_vol = sum(float(r.get("volume") or 0.0) for r in today_rows
                  if slot_key(str(r.get("session_time") or ""), tf_minutes) == latest_slot)

    prior = [r for r in rows if r.get("session_date") != latest_day]
    baseline = build_slot_baseline(prior, tf_minutes).get(latest_slot, [])
    res = slot_vsr(cur_vol, baseline)
    res.update({
        "slot": latest_slot,
        "tf_minutes": tf_minutes,
        "session": latest_day,
        "current_volume": round(cur_vol, 1),
        "source": "FUT+FUT2" if use_combined else "FUT",
        "label": classify_vsr(res.get("ratio"), thresholds),
        "coverage": {k: cov[k] for k in
                     ("n_sessions", "first_session", "last_session", "mean_coverage_pct")},
        "_method": "slot-normalised: compared against the SAME clock slot on prior "
                   "sessions, not a flat SMA, because NIFTY volume is U-shaped "
                   "across the day",
        "_evidence": "Only measure of 17 in backtest_ma_geometry.py with positive "
                     "directional advantage (+Rs262/trade). NOT yet a tradeable "
                     "edge: OOS n=117 over ~20 sessions in one regime, about "
                     "+Rs4/trade after top-5 concentration and slippage. "
                     "Re-test at ~120 sessions.",
    })
    return res
