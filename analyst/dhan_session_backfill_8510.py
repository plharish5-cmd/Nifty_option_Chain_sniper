"""Today's NIFTY 1m backfill via Dhan charts/intraday (rate-limit aware, read-only orders)."""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger("dhan_session_backfill_8510")

MAX_RETRIES = 3
BACKOFF_SEC = 3.0


def fetch_intraday_1m(
    *,
    base_url: str,
    access_token: str,
    client_id: str,
    session_day: str,
    sec_id: str = "13",
    segment: str = "IDX_I",
    instrument: str = "INDEX",
) -> Tuple[bool, List[Dict[str, Any]], str]:
    """One batched Dhan POST for today's session 09:15–15:30."""
    if not access_token or not client_id:
        return False, [], "DHAN credentials missing"
    url = f"{base_url.rstrip('/')}/charts/intraday"
    headers = {
        "access-token": access_token,
        "client-id": client_id,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "securityId": str(sec_id),
        "exchangeSegment": segment,
        "instrument": instrument,
        "interval": "1",
        "oi": False,
        "fromDate": f"{session_day} 09:15:00",
        "toDate": f"{session_day} 15:30:00",
    }
    last_err = ""
    for attempt in range(MAX_RETRIES):
        try:
            with httpx.Client(timeout=20.0) as client:
                resp = client.post(url, json=payload, headers=headers)
            if resp.status_code == 429:
                last_err = "Dhan rate limit 429"
                time.sleep(BACKOFF_SEC * (attempt + 1))
                continue
            if resp.status_code != 200:
                return False, [], f"HTTP {resp.status_code}: {resp.text[:160]}"
            data = resp.json()
            ts_list = data.get("timestamp") or data.get("start_time") or []
            rows: List[Dict[str, Any]] = []
            for ts, o, h, l, c, v in zip(
                ts_list,
                data.get("open") or [],
                data.get("high") or [],
                data.get("low") or [],
                data.get("close") or [],
                data.get("volume") or [0.0] * len(ts_list),
            ):
                rows.append(
                    {
                        "ts": int(ts),
                        "o": float(o),
                        "h": float(h),
                        "l": float(l),
                        "c": float(c),
                        "v": float(v or 0),
                    }
                )
            return True, rows, ""
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            last_err = str(exc)
            time.sleep(BACKOFF_SEC)
    return False, [], last_err or "backfill failed"


def upsert_spot_1m(market_db: Path, rows: List[Dict[str, Any]], sec_id: str = "13") -> int:
    if not rows or not market_db.exists():
        if not rows:
            return 0
        market_db.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with sqlite3.connect(market_db) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS candles_1m (
                sec_id TEXT, ts INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL,
                PRIMARY KEY (sec_id, ts)
            )
            """
        )
        for r in rows:
            conn.execute(
                "INSERT OR REPLACE INTO candles_1m (sec_id, ts, o, h, l, c, v) VALUES (?,?,?,?,?,?,?)",
                (sec_id, r["ts"], r["o"], r["h"], r["l"], r["c"], r["v"]),
            )
            n += 1
        conn.commit()
    return n


def backfill_today_session(
    backend: Any,
    session_day: Optional[str] = None,
) -> Dict[str, Any]:
    """Backfill today into market_db from Dhan; never raises."""
    session_day = session_day or datetime.now(IST).strftime("%Y-%m-%d")
    broker = getattr(backend, "broker", None)
    tok = getattr(broker, "access_token", None) or ""
    cid = getattr(broker, "client_id", None) or ""
    base = getattr(broker, "base_url", None) or "https://api.dhan.co/v2"
    ok, rows, err = fetch_intraday_1m(
        base_url=base, access_token=tok, client_id=cid, session_day=session_day
    )
    if not ok:
        log.warning("Dhan session backfill failed: %s", err)
        return {"ok": False, "error": err, "degraded": True}
    n = upsert_spot_1m(Path(backend.market_db), rows)
    return {"ok": True, "candles": n, "session_day": session_day}