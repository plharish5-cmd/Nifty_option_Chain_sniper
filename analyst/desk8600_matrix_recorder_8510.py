"""Record 8600 Matrix / Unified Tape bridge verdicts for future backtest — NO gating.

ADVISORY_ONLY: accumulated rows are not certified for live gates until held-out backtest passes.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

TABLE = "desk8600_matrix_log"
PROVENANCE = "recorded_live"

CREATE_SQL = f"""CREATE TABLE IF NOT EXISTS {TABLE} (
    id INTEGER PRIMARY KEY,
    session_date TEXT NOT NULL,
    interval_key TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    source_ts TEXT,
    spot REAL,
    direction TEXT,
    bias_label TEXT,
    conviction_grade TEXT,
    conviction_pct REAL,
    confirmed_flag INTEGER,
    pcr REAL,
    call_angle_deg REAL,
    put_angle_deg REAL,
    tape_verdict TEXT,
    provenance TEXT NOT NULL,
    bridge_evaluated_at TEXT,
    raw_rows_json TEXT,
    UNIQUE(session_date, interval_key)
)"""


def _row_by_group(rows: List[Dict[str, Any]], group: str) -> Optional[Dict[str, Any]]:
    for r in rows:
        if str(r.get("signal_group") or "") == group:
            return r
    return None


def _confirmed_flag(rows: List[Dict[str, Any]]) -> Optional[int]:
    """1 if any bridge verdict string contains Confirmed (▲/▼); 0 if divergent explicit; else null."""
    for r in rows:
        v = str(r.get("verdict") or "") + str(r.get("signal_name") or "")
        if "Confirmed" in v or "Confirmed ▲" in v or "Confirmed ▼" in v:
            return 1
        if "DIVERG" in v.upper() or "diverg" in v:
            return 0
    return None


def _conviction_from_rows(rows: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[float]]:
    hc = _row_by_group(rows, "High Conviction")
    if hc and hc.get("significant"):
        name = str(hc.get("verdict") or hc.get("signal_name") or "")
        return name[:32] if name else "HC", None
    burst = _row_by_group(rows, "Burst")
    if burst and burst.get("significant"):
        return "Burst", None
    return None, None


def snapshot_from_bridge(bridge_last: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Map bridge.last into one matrix row dict. Missing fields → null. Never raises."""
    if not isinstance(bridge_last, dict):
        return None
    rows = [x for x in (bridge_last.get("rows") or []) if isinstance(x, dict)]
    if not rows:
        return None
    tape = _row_by_group(rows, "State Change") or _row_by_group(rows, "tape_3m")
    interval_key = None
    source_ts = None
    if tape and tape.get("source_ts"):
        source_ts = str(tape["source_ts"])
        interval_key = source_ts
    if not interval_key:
        interval_key = str(bridge_last.get("evaluated_at") or "")
    if not interval_key:
        return None
    spot = None
    for r in rows:
        try:
            if r.get("spot") is not None:
                spot = float(r["spot"])
                break
        except (TypeError, ValueError):
            continue
    direction = str((tape or {}).get("direction") or "NEUTRAL").upper()
    verdict = str((tape or {}).get("verdict") or (tape or {}).get("signal_name") or "")
    grade, pct = _conviction_from_rows(rows)
    pcr_row = _row_by_group(rows, "PCR OI vs Futures")
    pcr = None
    if pcr_row:
        detail = str(pcr_row.get("detail") or pcr_row.get("verdict") or "")
        # Bridge does not expose numeric PCR on this row — store null unless future bridge adds it.
        _ = detail
    now = datetime.now(IST)
    session = now.strftime("%Y-%m-%d")
    if source_ts:
        try:
            session = datetime.fromisoformat(source_ts).astimezone(IST).strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            pass
    return {
        "session_date": session,
        "interval_key": interval_key,
        "recorded_at": now.isoformat(),
        "source_ts": source_ts,
        "spot": spot,
        "direction": direction if direction in ("CE", "PE", "NEUTRAL") else "NEUTRAL",
        "bias_label": verdict[:200] if verdict else None,
        "conviction_grade": grade,
        "conviction_pct": pct,
        "confirmed_flag": _confirmed_flag(rows),
        "pcr": pcr,
        "call_angle_deg": None,
        "put_angle_deg": None,
        "tape_verdict": verdict[:200] if verdict else None,
        "provenance": PROVENANCE,
        "bridge_evaluated_at": bridge_last.get("evaluated_at"),
        "raw_rows_json": json.dumps(rows, ensure_ascii=False, default=str),
    }


def record_matrix_snapshot(db_path: Any, snap: Dict[str, Any]) -> bool:
    """Insert one row if new interval; swallow errors. Returns True if inserted."""
    try:
        from pathlib import Path

        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path, timeout=2) as conn:
            conn.execute(CREATE_SQL)
            conn.execute(
                f"""INSERT OR IGNORE INTO {TABLE}
                (session_date, interval_key, recorded_at, source_ts, spot, direction, bias_label,
                 conviction_grade, conviction_pct, confirmed_flag, pcr, call_angle_deg, put_angle_deg,
                 tape_verdict, provenance, bridge_evaluated_at, raw_rows_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    snap["session_date"],
                    snap["interval_key"],
                    snap["recorded_at"],
                    snap.get("source_ts"),
                    snap.get("spot"),
                    snap.get("direction"),
                    snap.get("bias_label"),
                    snap.get("conviction_grade"),
                    snap.get("conviction_pct"),
                    snap.get("confirmed_flag"),
                    snap.get("pcr"),
                    snap.get("call_angle_deg"),
                    snap.get("put_angle_deg"),
                    snap.get("tape_verdict"),
                    snap.get("provenance"),
                    snap.get("bridge_evaluated_at"),
                    snap.get("raw_rows_json"),
                ),
            )
            return conn.total_changes > 0
    except Exception:
        return False


def count_matrix_rows_today(db_path: Any, session_date: Optional[str] = None) -> int:
    """Row count for cockpit display — never raises."""
    try:
        from pathlib import Path

        path = Path(db_path)
        if not path.exists():
            return 0
        day = session_date or datetime.now(IST).strftime("%Y-%m-%d")
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2) as conn:
            conn.execute(CREATE_SQL)
            row = conn.execute(
                f"SELECT COUNT(*) FROM {TABLE} WHERE session_date=?",
                (day,),
            ).fetchone()
            return int(row[0]) if row else 0
    except Exception:
        return 0