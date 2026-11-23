"""Close stale OPEN paper rows from prior session days so they never block new entries."""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger("prior_session_position_reconcile_8510")

PRIOR_SESSION_EXIT_REASON = "PRIOR_SESSION_RECONCILE — paper row from prior session day"


def entry_session_day(entry_time: Any) -> Optional[str]:
    """Parse YYYY-MM-DD from desk entry_time ('YYYY-MM-DD HH:MM:SS IST')."""
    raw = str(entry_time or "").strip()
    if len(raw) >= 10 and raw[4:5] == "-" and raw[7:8] == "-":
        return raw[:10]
    return None


def is_paper_ledger_mode(mode: Any) -> bool:
    """Paper ledger paths only — do not auto-close live-only rows."""
    m = str(mode or "PAPER_ONLY").upper()
    if m in ("LIVE_ONLY", "ALGO_LIVE_ONLY", "REAL"):
        return False
    return m in ("PAPER_ONLY", "PAPER", "BOTH", "")


def reconcile_prior_session_paper_positions(
    backend: Any,
    *,
    today: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Mark prior-day OPEN / EXIT_PENDING / EXIT_FAILED paper positions CLOSED at boot."""
    today = today or datetime.now(IST).strftime("%Y-%m-%d")
    now_str = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    closed: List[Dict[str, Any]] = []
    ledger = getattr(backend, "ledger_db", None)
    if not ledger:
        return closed
    try:
        with sqlite3.connect(ledger) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM positions
                WHERE status IN ('OPEN', 'EXIT_PENDING', 'EXIT_FAILED')
                """
            ).fetchall()
            for row in rows:
                p = dict(row)
                if not is_paper_ledger_mode(p.get("mode")):
                    continue
                ent_day = entry_session_day(p.get("entry_time"))
                if not ent_day or ent_day >= today:
                    continue
                pid = str(p.get("position_id") or "")
                if not pid:
                    continue
                conn.execute(
                    """
                    UPDATE positions
                    SET status='CLOSED',
                        exit_reason=?,
                        exit_time=?,
                        exit_price=COALESCE(NULLIF(exit_price, 0), current_ltp, entry_price)
                    WHERE position_id=?
                    """,
                    (PRIOR_SESSION_EXIT_REASON, now_str, pid),
                )
                closed.append(
                    {
                        "position_id": pid,
                        "entry_day": ent_day,
                        "strategy_id": p.get("strategy_id"),
                        "reason": PRIOR_SESSION_EXIT_REASON,
                    }
                )
            if closed:
                conn.commit()
    except sqlite3.Error as exc:
        log.warning("prior-session paper reconcile failed: %s", exc)
        return closed

    if not closed:
        return closed

    # Free lane / prototype memory tied to recovered rows.
    for item in closed:
        sid = str(item.get("strategy_id") or "").upper()
        try:
            from analyst.open_position_exit_sweep_8510 import _release_lane_after_close

            _release_lane_after_close(backend, {"strategy_id": sid, "position_id": item.get("position_id")})
        except Exception as exc:
            log.debug("lane release after prior-session reconcile: %s", exc)

    if hasattr(backend, "_sync_open_ema8_prototype_position"):
        backend._sync_open_ema8_prototype_position()

    log.warning("Prior-session reconcile closed %s paper position(s) before %s trading", len(closed), today)
    return closed